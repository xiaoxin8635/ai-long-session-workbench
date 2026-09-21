"""M-04 记忆抽取/检索管线测试（Fake LLM + Fake Embedding + 真实 PG，docs/06 §6）。

覆盖规格五用例：
  1. 注入事实 → 检索命中（含 hit_count 与 HIT 事件回写）
  2. 同 key 矛盾 → 旧条 superseded + 版本链完整
  3. 仲裁无法判定 → 双条 conflicted
  4. TTL 过期记忆不被检索返回
  5. 抽取超时 → 管线降级返回空、无写入
附加：MERGE 合并、低置信度过滤、同 key 检索去重、检索延迟（DoD < 300ms）。

Fake Embedding 用 1024 维单位正交基向量：任意两个不同基向量余弦相似度为 0，
同一向量为 1.0 —— 相似度完全可控且无需真实模型。
"""

import hashlib
import json
import math
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session_factory
from app.llm.client import LLMError, LLMUsage
from app.memory.conflict import ConflictResolver
from app.memory.deduplicator import MemoryDeduplicator
from app.memory.extractor import MemoryExtractor
from app.memory.pipeline import ExtractPipeline
from app.memory.retriever import MemoryRetriever
from app.models.enums import (
    MemberRole,
    MemoryEventType,
    MemoryStatus,
    MemoryType,
)
from app.models.memory import EMBEDDING_DIM, Memory, MemoryEvent
from app.repositories import memory_repo, user_repo, workspace_repo
from app.services.session_service import SessionService

# ---- Fake 组件 ----


def _basis(seed: int) -> list[float]:
    """构造单位正交基向量（第 seed 维为 1，其余 0）。"""
    vector = [0.0] * EMBEDDING_DIM
    vector[seed % EMBEDDING_DIM] = 1.0
    return vector


def _mixed(seed_a: int, seed_b: int, weight: float) -> list[float]:
    """构造与基 a 余弦相似度为 weight 的单位向量（a/b 为不同正交基序号）。

    Args:
        seed_a: 主基序号（目标相似度相对该基）。
        seed_b: 副基序号（与 a 正交，用于补齐模长）。
        weight: 与基 a 的目标余弦相似度（0~1）。
    """
    vector = [0.0] * EMBEDDING_DIM
    vector[seed_a % EMBEDDING_DIM] = weight
    vector[seed_b % EMBEDDING_DIM] = math.sqrt(1 - weight * weight)
    return vector


def _hash_vector(text: str) -> list[float]:
    """文本哈希 → 基向量（未注册文本的默认向量，不同文本近似正交）。"""
    digest = hashlib.md5(text.encode("utf-8")).digest()
    return _basis(int.from_bytes(digest[:2], "big"))


class FakeEmbedding:
    """可控向量的 Embedding fake：注册表优先，未注册走文本哈希基向量。"""

    def __init__(self) -> None:
        self._registered: dict[str, list[float]] = {}
        self.calls = 0

    def register(self, text: str, vector: list[float]) -> None:
        """为指定文本注册固定向量（构造高相似/零相似场景）。"""
        self._registered[text] = vector

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """按注册表或哈希返回向量（顺序与输入一致）。"""
        self.calls += 1
        return [self._registered.get(t) or _hash_vector(t) for t in texts]


class FakeJsonLLM:
    """按调用次序返回预设回复的 LLM fake（抽取与仲裁共用同一实例）。"""

    def __init__(self, replies: list[str]) -> None:
        """Args: replies: 依次弹出的预设回复（耗尽后复用最后一个）。"""
        self._replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    async def complete(
        self, messages: list[dict[str, str]], *, json_mode: bool = False
    ) -> tuple[str, LLMUsage]:
        """非流式补全：记录输入并返回预设回复。"""
        self.calls.append(messages)
        reply = self._replies[min(len(self.calls) - 1, len(self._replies) - 1)]
        return reply, LLMUsage(prompt_tokens=0, completion_tokens=0)


def _fact_json(key: str, content: str, **overrides: object) -> str:
    """构造单条抽取事实的 LLM JSON 回复。"""
    fact: dict[str, object] = {
        "key": key,
        "content": content,
        "memory_type": "semantic",
        "confidence": 0.9,
        "importance": 0.8,
        "ttl_days": None,
        "source_message_ids": [],
    }
    fact.update(overrides)
    return json.dumps({"facts": [fact]}, ensure_ascii=False)


# ---- 测试基建 ----


async def _prepare() -> tuple[object, uuid.UUID, uuid.UUID, uuid.UUID]:
    """创建 user/workspace/session，返回 (db, ws_id, user_id, session_id)。"""
    db = get_session_factory()()
    user = await user_repo.create(db, username=f"m04{uuid.uuid4().hex[:8]}", password_hash="x" * 60)
    ws = await workspace_repo.create(db, name="记忆管线测试", owner_id=user.id)
    await workspace_repo.add_member(db, ws_id=ws.id, user_id=user.id, role=MemberRole.OWNER)
    session = await SessionService().create(db, ws_id=ws.id, user=user, title="管线测试")
    await db.commit()
    return db, ws.id, user.id, session.id


def _pipeline(llm: FakeJsonLLM, embedding: FakeEmbedding) -> ExtractPipeline:
    """组装被测管线（fake LLM 同时充当抽取与仲裁模型）。"""
    return ExtractPipeline(
        extractor=MemoryExtractor(llm),  # type: ignore[arg-type]
        deduplicator=MemoryDeduplicator(),
        resolver=ConflictResolver(llm),  # type: ignore[arg-type]
        embedding=embedding,  # type: ignore[arg-type]
    )


def _round_messages(content: str) -> list[tuple[uuid.UUID, str, str]]:
    """构造一轮对话消息（user + assistant）。"""
    return [
        (uuid.uuid4(), "user", content),
        (uuid.uuid4(), "assistant", "好的，我记住了。"),
    ]


async def _active_memories(db: AsyncSession, ws_id: uuid.UUID) -> list[Memory]:
    """查询 workspace 下全部记忆条目（断言用）。"""
    result = await db.execute(select(Memory).where(Memory.workspace_id == ws_id))
    return list(result.scalars().all())


async def _events_of(db: AsyncSession, memory_id: uuid.UUID) -> list[MemoryEvent]:
    """查询指定记忆的事件流水（时间正序）。"""
    result = await db.execute(
        select(MemoryEvent)
        .where(MemoryEvent.memory_id == memory_id)
        .order_by(MemoryEvent.created_at)
    )
    return list(result.scalars().all())


# ---- 五用例 ----


async def test_extract_then_retrieve_hits() -> None:
    """用例 1：注入事实 → 新检索命中，hit_count 与 HIT 事件回写。"""
    db, ws_id, user_id, sid = await _prepare()
    try:
        llm = FakeJsonLLM([_fact_json("skill.langgraph", "用户熟悉 LangGraph")])
        embedding = FakeEmbedding()
        content_vector = _basis(3)
        embedding.register("用户熟悉 LangGraph", content_vector)

        written = await _pipeline(llm, embedding).run(
            db,
            ws_id=ws_id,
            user_id=user_id,
            session_id=sid,
            messages=_round_messages("我最近在用 LangGraph 开发"),
        )
        assert len(written) == 1
        assert written[0].key == "skill.langgraph"
        assert written[0].status == MemoryStatus.ACTIVE
        assert written[0].embedding == content_vector

        # 检索：query 向量与该条记忆同向 → 必然命中
        query = "用户的技术栈是什么"
        embedding.register(query, content_vector)
        retriever = MemoryRetriever(embedding)  # type: ignore[arg-type]
        hits = await retriever.retrieve(db, ws_id=ws_id, user_id=user_id, query=query)
        assert len(hits) == 1
        assert hits[0].memory.key == "skill.langgraph"
        assert hits[0].similarity > 0.99
        await db.commit()  # 落 hit_count

        # bulk update 不刷新 identity map，直接查列值断言
        hit_count = (
            await db.execute(select(Memory.hit_count).where(Memory.id == written[0].id))
        ).scalar_one()
        assert hit_count == 1
        events = await _events_of(db, written[0].id)
        assert MemoryEventType.HIT in [e.event_type for e in events]
        assert MemoryEventType.CREATED in [e.event_type for e in events]
    finally:
        await db.close()


async def test_conflict_supersede_builds_version_chain() -> None:
    """用例 2：同 key 矛盾且新事实可信 → 旧条 superseded + 新条挂版本链。"""
    db, ws_id, user_id, sid = await _prepare()
    try:
        llm = FakeJsonLLM(
            [
                _fact_json("skill.frontend", "用户不熟悉前端"),  # 第一次：抽取
                '{"action": "supersede"}',  # 第二次：仲裁
            ]
        )
        embedding = FakeEmbedding()
        # 直接预置旧记忆（保证本轮抽取+仲裁恰好对应两次预设回复）
        old = await memory_repo.create_memory(
            db,
            ws_id=ws_id,
            user_id=user_id,
            memory_type=MemoryType.SEMANTIC,
            key="skill.frontend",
            content="用户熟悉前端开发",
            confidence=0.7,
            importance=0.5,
            embedding=_basis(1),
        )
        await db.commit()

        written = await _pipeline(llm, embedding).run(
            db,
            ws_id=ws_id,
            user_id=user_id,
            session_id=sid,
            messages=_round_messages("其实我早就不写前端了"),
        )
        assert len(written) == 1
        new = written[0]
        assert new.status == MemoryStatus.ACTIVE
        assert new.supersedes_id == old.id
        assert old.status == MemoryStatus.SUPERSEDED

        old_events = await _events_of(db, old.id)
        assert MemoryEventType.SUPERSEDED in [e.event_type for e in old_events]
        new_events = await _events_of(db, new.id)
        assert [e.event_type for e in new_events] == [MemoryEventType.CREATED]
    finally:
        await db.close()


async def test_conflict_coexist_marks_both_conflicted() -> None:
    """用例 3：仲裁无法判定 → 双条 conflicted 待用户裁决。"""
    db, ws_id, user_id, sid = await _prepare()
    try:
        llm = FakeJsonLLM(
            [
                _fact_json("diet.preference", "用户吃素"),  # 抽取
                '{"action": "coexist"}',  # 仲裁
            ]
        )
        embedding = FakeEmbedding()
        old = await memory_repo.create_memory(
            db,
            ws_id=ws_id,
            user_id=user_id,
            memory_type=MemoryType.SEMANTIC,
            key="diet.preference",
            content="用户无饮食限制",
            confidence=0.6,
            importance=0.4,
            embedding=_basis(2),
        )
        await db.commit()

        written = await _pipeline(llm, embedding).run(
            db,
            ws_id=ws_id,
            user_id=user_id,
            session_id=sid,
            messages=_round_messages("我是素食主义者"),
        )
        assert len(written) == 1
        new = written[0]
        assert new.status == MemoryStatus.CONFLICTED
        assert old.status == MemoryStatus.CONFLICTED
        for memory in (old, new):
            events = await _events_of(db, memory.id)
            assert MemoryEventType.CONFLICT in [e.event_type for e in events]
    finally:
        await db.close()


async def test_conflict_independent_keeps_both_active() -> None:
    """用例 3b：同主题不矛盾的并行事实 → INDEPENDENT 双条 ACTIVE 并存、不打冲突标。"""
    db, ws_id, user_id, sid = await _prepare()
    try:
        llm = FakeJsonLLM(
            [
                _fact_json("schedule.availability", "用户每天晚上复习两小时"),  # 抽取
                '{"action": "independent"}',  # 仲裁
            ]
        )
        embedding = FakeEmbedding()
        old = await memory_repo.create_memory(
            db,
            ws_id=ws_id,
            user_id=user_id,
            memory_type=MemoryType.PROCEDURAL,
            key="schedule.availability",
            content="用户每天刷两道算法题",
            confidence=0.8,
            importance=0.6,
            embedding=_basis(2),
        )
        await db.commit()

        written = await _pipeline(llm, embedding).run(
            db,
            ws_id=ws_id,
            user_id=user_id,
            session_id=sid,
            messages=_round_messages("我每天晚上都会复习两个小时"),
        )
        assert len(written) == 1
        new = written[0]
        assert new.status == MemoryStatus.ACTIVE
        assert new.supersedes_id is None
        assert old.status == MemoryStatus.ACTIVE
        # 双方都不产生 CONFLICT/SUPERSEDED 事件（各自仅有预置/本次的 CREATED）
        assert [e.event_type for e in await _events_of(db, new.id)] == [MemoryEventType.CREATED]
        assert [e.event_type for e in await _events_of(db, old.id)] == [MemoryEventType.CREATED]
    finally:
        await db.close()


def test_arbitrate_prompt_documents_independent() -> None:
    """仲裁 prompt 必须文档化 independent 动作及其与 coexist 的边界（防 prompt 回退）。"""
    from app.memory.prompts import ARBITRATE_SYSTEM

    assert "independent" in ARBITRATE_SYSTEM
    assert "互不矛盾" in ARBITRATE_SYSTEM
    # 抽取 prompt 必须约束"互不矛盾的事实禁止共用 key"
    from app.memory.prompts import EXTRACT_SYSTEM

    assert "禁止共用同一个 key" in EXTRACT_SYSTEM
    # fix_v11_mh 回归：问句脑补"用户未提供 X"污染既有 key（28 对 conflicted
    # 的根因）——抽取 prompt 必须显式禁止从问句推断否定性事实
    assert "禁止推断" in EXTRACT_SYSTEM and "否定性事实" in EXTRACT_SYSTEM


def test_retriever_weights_hit_matthew_clamp() -> None:
    """fix_v10_mh 回归：hit 权重必须受 sim 钳制，防累积量碾压每查询量。

    fix_v10_mh（100 行全量首跑）实锤：hit≥20 即饱和满分的旧设计下，
    700 次检索让头部"万金油"条目 hit 200~500 恒拿 0.15，sim 0.74 的
    目标条目（hit=0）以 0.006 分差被挤出 top8，recall 崩至 0.22。
    约束：hit 满分差（_W_HIT）必须小于典型 sim 差（≥0.15）的加权和，
    使"高 sim 冷条目"能稳定翻越"低 sim 高频条目"。
    """
    from app.memory import retriever as _retriever

    assert _retriever._W_HIT < _retriever._W_SIMILARITY
    assert _retriever._W_HIT < 0.15 * _retriever._W_SIMILARITY


async def test_expired_memory_not_retrieved() -> None:
    """用例 4：TTL 过期记忆不被检索返回（软过期过滤）。"""
    db, ws_id, user_id, sid = await _prepare()
    try:
        embedding = FakeEmbedding()
        live = await memory_repo.create_memory(
            db,
            ws_id=ws_id,
            user_id=user_id,
            memory_type=MemoryType.SEMANTIC,
            key="job.状态",
            content="在职",
            confidence=0.9,
            importance=0.8,
            embedding=_basis(5),
        )
        expired = await memory_repo.create_memory(
            db,
            ws_id=ws_id,
            user_id=user_id,
            memory_type=MemoryType.SEMANTIC,
            key="job.进度",
            content="三面通过",
            confidence=0.9,
            importance=0.8,
            embedding=_basis(6),
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        await db.commit()
        assert live.id != expired.id

        query = "我的求职进度"
        embedding.register(query, _basis(6))  # 与过期条目同向
        retriever = MemoryRetriever(embedding)  # type: ignore[arg-type]
        hits = await retriever.retrieve(db, ws_id=ws_id, user_id=user_id, query=query)
        assert all(h.memory.id != expired.id for h in hits)
        # 过期条目被过滤后，次近邻（在职）仍可返回
        assert [h.memory.key for h in hits] == ["job.状态"]
    finally:
        await db.close()


async def test_extract_failure_degrades_to_noop() -> None:
    """用例 5：抽取 LLM 超时 → 管线返回空、无写入、不外抛。"""
    db, ws_id, user_id, sid = await _prepare()
    try:

        class FailingLLM(FakeJsonLLM):
            """complete 直接抛 LLMError（模拟上游超时）。"""

            async def complete(
                self, messages: list[dict[str, str]], *, json_mode: bool = False
            ) -> tuple[str, LLMUsage]:
                raise LLMError("上游超时（测试注入）")

        embedding = FakeEmbedding()
        written = await _pipeline(FailingLLM([]), embedding).run(  # type: ignore[arg-type]
            db,
            ws_id=ws_id,
            user_id=user_id,
            session_id=sid,
            messages=_round_messages("我熟悉 LangGraph"),
        )
        assert written == []
        assert await _active_memories(db, ws_id) == []
        assert embedding.calls == 0  # 抽取失败不再走向量化
    finally:
        await db.close()


# ---- 附加覆盖 ----


async def test_merge_updates_old_memory() -> None:
    """语义一致 → MERGE：旧条目 content/confidence 更新、version 自增、不新建。"""
    db, ws_id, user_id, sid = await _prepare()
    try:
        llm = FakeJsonLLM(
            [
                _fact_json("skill.langgraph", "用户熟悉 LangGraph 与 LangSmith"),
                '{"action": "merge", "merged_content": "用户熟悉 LangGraph 及其生态（LangSmith）", '
                '"merged_confidence": 0.95}',
            ]
        )
        embedding = FakeEmbedding()
        old = await memory_repo.create_memory(
            db,
            ws_id=ws_id,
            user_id=user_id,
            memory_type=MemoryType.SEMANTIC,
            key="skill.langgraph",
            content="用户熟悉 LangGraph",
            confidence=0.8,
            importance=0.7,
            embedding=_basis(7),
        )
        await db.commit()

        written = await _pipeline(llm, embedding).run(
            db,
            ws_id=ws_id,
            user_id=user_id,
            session_id=sid,
            messages=_round_messages("我还用 LangSmith 做观测"),
        )
        assert written == [old]  # 合并写回旧条目
        assert old.content == "用户熟悉 LangGraph 及其生态（LangSmith）"
        assert old.confidence == 0.95
        assert old.version == 2
        memories = await _active_memories(db, ws_id)
        assert len(memories) == 1  # 未新建条目
        events = await _events_of(db, old.id)
        assert MemoryEventType.MERGED in [e.event_type for e in events]
    finally:
        await db.close()


async def test_low_confidence_facts_filtered() -> None:
    """低于 extract_min_confidence（0.5）的抽取结果被丢弃（控噪）。"""
    db, ws_id, user_id, sid = await _prepare()
    try:
        llm = FakeJsonLLM(
            [
                _fact_json("chat.smalltalk", "用户今天心情不错", confidence=0.2),
            ]
        )
        written = await _pipeline(llm, FakeEmbedding()).run(
            db,
            ws_id=ws_id,
            user_id=user_id,
            session_id=sid,
            messages=_round_messages("今天天气真好"),
        )
        assert written == []
        assert await _active_memories(db, ws_id) == []
    finally:
        await db.close()


async def test_retrieval_same_key_keeps_best_score() -> None:
    """候选含同 key 多条时，重排后只保留综合分最高的一条。"""
    db, ws_id, user_id, sid = await _prepare()
    try:
        # 两条同 key 不同正文，向量与 query 同向；importance 区分综合分
        await memory_repo.create_memory(
            db,
            ws_id=ws_id,
            user_id=user_id,
            memory_type=MemoryType.SEMANTIC,
            key="profile.city",
            content="用户住在杭州",
            confidence=0.9,
            importance=0.9,
            embedding=_basis(9),
        )
        await memory_repo.create_memory(
            db,
            ws_id=ws_id,
            user_id=user_id,
            memory_type=MemoryType.SEMANTIC,
            key="profile.city",
            content="用户住在浙江杭州",
            confidence=0.9,
            importance=0.2,
            embedding=_basis(9),
        )
        await db.commit()

        query = "用户住在哪座城市"
        embedding = FakeEmbedding()
        embedding.register(query, _basis(9))
        hits = await MemoryRetriever(embedding).retrieve(  # type: ignore[arg-type]
            db, ws_id=ws_id, user_id=user_id, query=query
        )
        assert len(hits) == 1
        assert hits[0].memory.content == "用户住在杭州"  # importance 高者胜出
    finally:
        await db.close()


async def test_retrieval_latency_under_300ms() -> None:
    """DoD：50 条记忆规模下单次检索（不含 LLM）延迟 < 300ms。"""
    db, ws_id, user_id, sid = await _prepare()
    try:
        for i in range(50):
            await memory_repo.create_memory(
                db,
                ws_id=ws_id,
                user_id=user_id,
                memory_type=MemoryType.SEMANTIC,
                key=f"topic.{i}",
                content=f"测试记忆条目 {i}",
                confidence=0.9,
                importance=0.5,
                embedding=_basis(i),
            )
        await db.commit()

        embedding = FakeEmbedding()
        embedding.register("查询", _basis(42))
        retriever = MemoryRetriever(embedding)  # type: ignore[arg-type]
        start = time.perf_counter()
        hits = await retriever.retrieve(db, ws_id=ws_id, user_id=user_id, query="查询")
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert len(hits) == 8  # top_k 默认 8
        assert elapsed_ms < 300, f"检索延迟 {elapsed_ms:.1f}ms 超标"
    finally:
        await db.close()


async def test_retriever_degrades_without_embedding_service() -> None:
    """Embedding 服务不可用 → 检索降级返回空列表（不抛异常）。"""
    from app.llm.embeddings import EmbeddingError

    db, ws_id, user_id, sid = await _prepare()
    try:

        class BrokenEmbedding(FakeEmbedding):
            """embed 直接抛 EmbeddingError（模拟服务不可用）。"""

            async def embed(self, texts: list[str]) -> list[list[float]]:
                raise EmbeddingError("服务不可用（测试注入）")

        hits = await MemoryRetriever(BrokenEmbedding()).retrieve(  # type: ignore[arg-type]
            db, ws_id=ws_id, user_id=user_id, query="任意"
        )
        assert hits == []
    finally:
        await db.close()


# ---- 评测优化轮（Fix A/B/D）----


async def test_known_keys_injected_into_extract_prompt() -> None:
    """Fix B：既有 active key 注入抽取 prompt，引导 LLM 复用命名。"""
    db, ws_id, user_id, sid = await _prepare()
    try:
        await memory_repo.create_memory(
            db,
            ws_id=ws_id,
            user_id=user_id,
            memory_type=MemoryType.SEMANTIC,
            key="contact.phone",
            content="用户手机号 13800000001",
            confidence=0.9,
            importance=0.8,
            embedding=_basis(0),
        )
        await db.commit()

        llm = FakeJsonLLM([_fact_json("contact.email", "用户邮箱 a@b.com")])
        embedding = FakeEmbedding()
        embedding.register("用户邮箱 a@b.com", _basis(1))  # 与旧条正交：不触发冲突路径
        written = await _pipeline(llm, embedding).run(
            db,
            ws_id=ws_id,
            user_id=user_id,
            session_id=sid,
            messages=_round_messages("我邮箱是 a@b.com"),
        )
        assert len(written) == 1
        # 抽取调用的 system prompt 中出现既有 key（复用规则生效）
        system = llm.calls[0][0]["content"]
        assert "contact.phone" in system
        assert "复用已有 key" in system
    finally:
        await db.close()


async def test_suspect_conflict_layer_supersedes() -> None:
    """Fix A：疑似冲突层（0.80 ≤ sim < 0.92）命中 → 仲裁 supersede 生效。"""
    db, ws_id, user_id, sid = await _prepare()
    try:
        llm = FakeJsonLLM(
            [
                _fact_json("profile.phone", "用户手机号 13911112222"),
                '{"action": "supersede"}',
            ]
        )
        embedding = FakeEmbedding()
        # 新事实与旧条相似度 0.85：低于判重阈值（不 MERGE）但落入疑似冲突层
        suspect_vector = _mixed(10, 11, 0.85)
        embedding.register("用户手机号 13911112222", suspect_vector)
        old = await memory_repo.create_memory(
            db,
            ws_id=ws_id,
            user_id=user_id,
            memory_type=MemoryType.SEMANTIC,
            key="contact.phone",
            content="用户手机号 13800000001",
            confidence=0.8,
            importance=0.7,
            embedding=_basis(10),
        )
        await db.commit()

        written = await _pipeline(llm, embedding).run(
            db,
            ws_id=ws_id,
            user_id=user_id,
            session_id=sid,
            messages=_round_messages("我换手机号了，13911112222"),
        )
        assert len(written) == 1
        new = written[0]
        assert new.status == MemoryStatus.ACTIVE
        assert new.supersedes_id == old.id  # 版本链挂上：仲裁 supersede 生效
        old_status = (
            await db.execute(select(Memory.status).where(Memory.id == old.id))
        ).scalar_one()
        assert old_status == MemoryStatus.SUPERSEDED
    finally:
        await db.close()


async def test_suspect_conflict_merge_downgraded_to_coexist() -> None:
    """Fix A：疑似冲突层命中且仲裁判 merge → 降级 COEXIST（防误合并）。"""
    db, ws_id, user_id, sid = await _prepare()
    try:
        llm = FakeJsonLLM(
            [
                _fact_json("profile.city.detail", "用户常住在杭州西湖区"),
                '{"action": "merge", "merged_content": "合并内容", "merged_confidence": 0.9}',
            ]
        )
        embedding = FakeEmbedding()
        suspect_vector = _mixed(20, 21, 0.85)
        embedding.register("用户常住在杭州西湖区", suspect_vector)
        old = await memory_repo.create_memory(
            db,
            ws_id=ws_id,
            user_id=user_id,
            memory_type=MemoryType.SEMANTIC,
            key="profile.city",
            content="用户住在杭州",
            confidence=0.8,
            importance=0.7,
            embedding=_basis(20),
        )
        await db.commit()

        written = await _pipeline(llm, embedding).run(
            db,
            ws_id=ws_id,
            user_id=user_id,
            session_id=sid,
            messages=_round_messages("我常住在西湖区"),
        )
        assert len(written) == 1
        new = written[0]
        # 双条 conflicted：MERGE 被降级，未写入 merged_content
        assert new.status == MemoryStatus.CONFLICTED
        assert new.content == "用户常住在杭州西湖区"
        old_content = (
            await db.execute(select(Memory.content).where(Memory.id == old.id))
        ).scalar_one()
        assert old_content == "用户住在杭州"  # 旧条未被合并改写
        assert new.supersedes_id is None
    finally:
        await db.close()


class _FakeDeadlockOrig(Exception):
    """模拟 asyncpg 死锁原始异常（sqlstate=40P01，供 _is_deadlock 识别）。"""

    sqlstate = "40P01"


async def test_deadlock_detected_retried_once() -> None:
    """Fix D：upsert 死锁异常 → 回滚重试一次后成功写入。"""
    db, ws_id, user_id, sid = await _prepare()
    try:

        class FlakyPipeline(ExtractPipeline):
            """首次 upsert 抛 PG 死锁 DBAPIError 的被测管线。"""

            def __init__(self, **kwargs: object) -> None:
                super().__init__(**kwargs)  # type: ignore[arg-type]
                self.upsert_calls = 0

            async def _upsert_one(self, db: AsyncSession, **kwargs: object) -> Memory | None:
                """首次调用注入死锁异常，之后走正常路径。"""
                self.upsert_calls += 1
                if self.upsert_calls == 1:
                    raise DBAPIError("模拟死锁（测试注入）", None, _FakeDeadlockOrig())
                return await super()._upsert_one(db, **kwargs)  # type: ignore[arg-type]

        llm = FakeJsonLLM([_fact_json("skill.langgraph", "用户熟悉 LangGraph")])
        embedding = FakeEmbedding()
        pipeline = FlakyPipeline(
            extractor=MemoryExtractor(llm),
            deduplicator=MemoryDeduplicator(),
            resolver=ConflictResolver(llm),
            embedding=embedding,
        )
        written = await pipeline.run(
            db,
            ws_id=ws_id,
            user_id=user_id,
            session_id=sid,
            messages=_round_messages("我最近在用 LangGraph 开发"),
        )
        assert pipeline.upsert_calls == 2  # 首次失败 + 重试成功
        assert len(written) == 1
        assert written[0].status == MemoryStatus.ACTIVE
    finally:
        await db.close()


async def test_retrieve_degrades_when_hit_write_deadlocks(monkeypatch) -> None:
    """Fix 轮加固：命中写回遇 PG 死锁（40P01）降级为告警，检索结果照常返回。

    实机取证：record_hits 批量 UPDATE 与抽取管线交叉死锁曾把整个 chat
    请求炸成 502 / preview 500——命中统计是增强项，绝不阻断检索主链路。
    """
    db, ws_id, user_id, sid = await _prepare()
    try:
        await memory_repo.create_memory(
            db,
            ws_id=ws_id,
            user_id=user_id,
            memory_type=MemoryType.SEMANTIC,
            key="profile.city",
            content="用户住在杭州",
            confidence=0.9,
            importance=0.8,
            embedding=_basis(0),
        )
        await db.commit()

        async def _deadlock(*args: object, **kwargs: object) -> object:
            """替换 record_hits：模拟与抽取管线交叉成环的死锁。"""
            raise DBAPIError("模拟命中写回死锁（测试注入）", None, _FakeDeadlockOrig())

        monkeypatch.setattr(memory_repo, "record_hits", _deadlock)
        embedding = FakeEmbedding()
        embedding.register("用户住在哪座城市", _basis(0))
        hits = await MemoryRetriever(embedding).retrieve(  # type: ignore[arg-type]
            db, ws_id=ws_id, user_id=user_id, query="用户住在哪座城市"
        )
        assert len(hits) == 1  # 统计写失败不阻断检索
    finally:
        await db.close()


async def test_record_hits_out_of_order_ids_persisted() -> None:
    """record_hits 乱序传入 ID 按内部排序逐行更新：hit_count 全 +1 且 HIT 事件齐全。"""
    db, ws_id, user_id, sid = await _prepare()
    try:
        first = await memory_repo.create_memory(
            db,
            ws_id=ws_id,
            user_id=user_id,
            memory_type=MemoryType.SEMANTIC,
            key="a.first",
            content="第一条",
            confidence=0.9,
            importance=0.5,
        )
        second = await memory_repo.create_memory(
            db,
            ws_id=ws_id,
            user_id=user_id,
            memory_type=MemoryType.SEMANTIC,
            key="b.second",
            content="第二条",
            confidence=0.9,
            importance=0.5,
        )
        await db.commit()

        updated = await memory_repo.record_hits(db, [second.id, first.id])  # 故意乱序
        await db.commit()
        assert updated == 2
        for mid in (first.id, second.id):
            row = (
                await db.execute(
                    select(Memory.hit_count, Memory.last_hit_at).where(Memory.id == mid)
                )
            ).one()
            assert row.hit_count == 1
            assert row.last_hit_at is not None
        assert MemoryEventType.HIT in [e.event_type for e in await _events_of(db, first.id)]
    finally:
        await db.close()


async def test_conflicted_entries_retrievable_with_penalty() -> None:
    """CONFLICTED 待裁决条目参与检索且降权（Fix A 副作用修复）。

    待裁决期间完全排除会使信息从上下文静默消失；修复后同条件（相似度/
    importance 一致）下 CONFLICTED 仍可召回，但综合分低于 ACTIVE、排名靠后。
    """
    db, ws_id, user_id, sid = await _prepare()
    try:
        active = await memory_repo.create_memory(
            db,
            ws_id=ws_id,
            user_id=user_id,
            memory_type=MemoryType.SEMANTIC,
            key="profile.city",
            content="用户住在杭州",
            confidence=0.9,
            importance=0.9,
            embedding=_basis(9),
        )
        conflicted = await memory_repo.create_memory(
            db,
            ws_id=ws_id,
            user_id=user_id,
            memory_type=MemoryType.SEMANTIC,
            key="profile.region",
            content="用户住在浙江",
            confidence=0.9,
            importance=0.9,
            embedding=_basis(9),
        )
        conflicted.status = MemoryStatus.CONFLICTED
        await db.commit()

        embedding = FakeEmbedding()
        embedding.register("用户住在哪座城市", _basis(9))
        hits = await MemoryRetriever(embedding).retrieve(  # type: ignore[arg-type]
            db, ws_id=ws_id, user_id=user_id, query="用户住在哪座城市"
        )
        # 两条不同 key 均召回；CONFLICTED 参与但排 ACTIVE 之后
        assert [(h.memory.status, h.memory.key) for h in hits] == [
            (MemoryStatus.ACTIVE, active.key),
            (MemoryStatus.CONFLICTED, conflicted.key),
        ]
        assert hits[0].score > hits[1].score  # 惩罚系数生效
    finally:
        await db.close()
