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
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
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
