"""M-05 Context Builder 测试（docs/06 §7）。

覆盖四层：
  - 纯函数装配：区块内 score 淘汰 / working 滑窗 / 全局 evict_order 防线 /
    渲染结构与总量有界
  - 预算加载：真实 config/budgets YAML 校验与换算 / 未知 profile 报错
  - build 取数：滚动摘要直取 + fake 检索注入 + 同 key 摘要去重
  - preview 端点：装配明细往返 / 鉴权与隔离 / 非法输入
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from app.context.budget import BudgetConfigError, ContextBudget, load_budget
from app.context.builder import ContextBuilder
from app.context.schemas import (
    AssembledContext,
    ContextCandidates,
    ContextItem,
    SectionKey,
)
from app.core.deps import get_redis
from app.db.session import get_session_factory
from app.memory.schemas import ScoredMemory
from app.memory.working_memory import WorkingMemory
from app.models.enums import MemberRole, MemoryType
from app.models.memory import Memory
from app.repositories import session_repo, user_repo, workspace_repo
from app.repositories.memory_repo import summary_memory_key
from app.services.session_service import SessionService

_PASSWORD = "passw0rd123"


def _make_budget(window: int = 600, **overrides: int) -> ContextBudget:
    """构造测试用小预算（窗口与各区块绝对 token 数可覆盖）。

    默认口径：window=600 / reserve=60（available=540），区块预算足够容纳
    少量短条目；淘汰类用例按需覆盖更小的窗口或区块预算。

    Args:
        window: 总窗口 token 数（reserve 固定为 60）。
        **overrides: 覆盖指定区块的预算（键为 SectionKey 值字符串）。
    """
    sections: dict[SectionKey, int] = {
        SectionKey.SYSTEM: -1,
        SectionKey.PROCEDURAL: 30,
        SectionKey.SEMANTIC: 30,
        SectionKey.EPISODIC: 30,
        SectionKey.WORKING: 40,
        SectionKey.RAG: 30,
        SectionKey.TOOL_RESULTS: 30,
    }
    for key, value in overrides.items():
        sections[SectionKey(key)] = value
    return ContextBudget(
        window_tokens=window,
        reserve_output=60,
        sections=sections,
        evict_order=(
            SectionKey.RAG,
            SectionKey.EPISODIC,
            SectionKey.SEMANTIC,
            SectionKey.WORKING,
            SectionKey.PROCEDURAL,
        ),
        profile="unit-test",
    )


def _usage(result: AssembledContext, key: SectionKey) -> int:
    """取装配结果中某区块的实际 token 占用。"""
    return next(u.tokens for u in result.sections if u.key is key)


def _item(content: str, source: str, score: float) -> ContextItem:
    """构造候选片段（中文内容 token = 字符数，便于精确控预算）。"""
    return ContextItem(content=content, source=source, score=score)


# ---- 纯函数装配 ----


def test_score_eviction_keeps_high_score() -> None:
    """区块内淘汰：超预算时按 score 降序装填，低分条目被丢弃。"""
    candidates = ContextCandidates(
        system_prompt="系统提示",
        semantic=(
            _item("低" * 20, "fact.low", score=0.3),
            _item("高" * 20, "fact.high", score=0.9),
        ),
    )
    result = ContextBuilder.assemble(candidates, _make_budget(semantic=25))

    # 25 token 只装得下一条：高分保留、低分丢弃
    system_msg = result.messages[0]["content"]
    assert "fact.high" in system_msg
    assert "fact.low" not in system_msg
    usage = next(u for u in result.sections if u.key is SectionKey.SEMANTIC)
    assert usage.included == 1
    assert usage.dropped == 1


def test_working_sliding_window_keeps_latest() -> None:
    """working 淘汰：从最旧滑窗，末条（当前问题）始终保留。"""
    working = (
        {"role": "user", "content": "旧" * 30},  # 最旧，超预算时先丢
        {"role": "assistant", "content": "答" * 30},
        {"role": "user", "content": "新问题"},  # 末条必留
    )
    candidates = ContextCandidates(system_prompt="系统提示", working=working)
    result = ContextBuilder.assemble(candidates, _make_budget())

    roles = [(m["role"], m["content"]) for m in result.messages[1:]]
    assert roles == [("assistant", "答" * 30), ("user", "新问题")]
    usage = next(u for u in result.sections if u.key is SectionKey.WORKING)
    assert usage.included == 2
    assert usage.dropped == 1


def test_global_evict_order_clears_rag_first() -> None:
    """全局防线：总量超 available 时按 evict_order 整区块放弃（rag 最先）。"""
    candidates = ContextCandidates(
        system_prompt="系" * 30,
        rag=(_item("知" * 30, "chunk:1", score=0.9),),
        procedural=(_item("好" * 30, "pref.x", score=0.9),),
        working=({"role": "user", "content": "问"},),
    )
    # available = 100 - 60 = 40 < system(30)+rag(30)+procedural(30)+working(1) = 91
    budget = _make_budget(window=100)
    result = ContextBuilder.assemble(candidates, budget)

    # evict_order 首位 rag 清空；仍超限则继续清 semantic/…/procedural
    assert _usage(result, SectionKey.RAG) == 0
    assert _usage(result, SectionKey.PROCEDURAL) == 0
    # working 末条不可丢；总量回到 available 内
    assert result.messages[-1]["content"] == "问"
    assert result.total_tokens <= budget.available_tokens


def test_render_structure_and_empty_sections() -> None:
    """渲染：一条合成 system（结构化标签可溯源）+ working 原文 role 消息。"""
    candidates = ContextCandidates(
        system_prompt="系统提示",
        procedural=(_item("偏好深色主题", "pref.theme", 0.9),),
        semantic=(_item("用户住在杭州", "profile.city", 0.8),),
        episodic=(_item("早前讨论过部署", "session:old", 0.5),),
        working=({"role": "user", "content": "当前问题"},),
    )
    result = ContextBuilder.assemble(candidates, _make_budget())

    assert [m["role"] for m in result.messages] == ["system", "user"]
    system_msg = result.messages[0]["content"]
    assert system_msg.startswith("系统提示")
    assert '<memory type="procedural" source="pref.theme">偏好深色主题</memory>' in system_msg
    assert '<memory type="semantic" source="profile.city">用户住在杭州</memory>' in system_msg
    assert '<summary source="session:old">早前讨论过部署</summary>' in system_msg
    assert result.messages[1]["content"] == "当前问题"

    # 空区块不渲染包裹标签（rag/tool_results 未给候选）
    assert "<knowledge_chunks>" not in system_msg
    assert "<tool_results>" not in system_msg


def test_procedural_wrapper_carries_behavior_directive() -> None:
    """procedural 包裹标签内注入行为引导语（长对话偏好遵循补丁）。

    引导语须位于包裹开标签与首条记忆之间；semantic 区块无引导语不受影响。
    """
    candidates = ContextCandidates(
        system_prompt="系统提示",
        procedural=(_item("偏好简洁回答", "pref.style", 0.9),),
        semantic=(_item("用户住在杭州", "profile.city", 0.8),),
        working=({"role": "user", "content": "问"},),
    )
    result = ContextBuilder.assemble(candidates, _make_budget())
    system_msg = result.messages[0]["content"]

    assert "必须逐条遵守" in system_msg
    assert (
        system_msg.index("<procedural_memories>")
        < system_msg.index("必须逐条遵守")
        < system_msg.index('<memory type="procedural"')
    )
    # semantic 区块无引导语（不重复注入指令）
    semantic_start = system_msg.index("<semantic_memories>")
    assert "必须逐条遵守" not in system_msg[semantic_start:]


def test_assemble_total_bounded_under_available() -> None:
    """总量有界：候选远超预算时装配结果仍 ≤ available（全局防线兜底）。"""
    big = [
        _item("事" * 25, f"fact.{i}", score=1.0 - i * 0.01) for i in range(20)
    ]  # 20 × 25 = 500 token semantic 候选
    candidates = ContextCandidates(
        system_prompt="系" * 50,
        semantic=tuple(big),
        rag=(_item("知" * 25, "chunk:1", 0.9),),
        working=({"role": "user", "content": "问" * 25}, {"role": "user", "content": "新"}),
    )
    # 区块裁剪后 50+25+25+26=126 > available(180-60=120) → 全局防线清空 rag
    budget = _make_budget(window=180)
    result = ContextBuilder.assemble(candidates, budget)

    assert result.total_tokens <= budget.available_tokens
    assert _usage(result, SectionKey.RAG) == 0
    # 末条（当前问题）不可丢
    assert result.messages[-1]["content"] == "新"


# ---- 预算加载（真实 config/budgets YAML，经仓库根目录探测） ----


def test_load_budget_default_and_memory_first() -> None:
    """三份内置 profile 均合法：占比换算正确、system 不裁剪、总预算不超 available。"""
    default = load_budget("default")
    assert default.window_tokens == 4000
    assert default.sections[SectionKey.SYSTEM] == -1
    body = {k: v for k, v in default.sections.items() if k is not SectionKey.SYSTEM}
    assert sum(body.values()) <= default.available_tokens
    assert default.available_tokens == 4000 - 400

    memory_first = load_budget("memory_first")
    # memory_first 语义记忆预算（900）应大于 default（540）；evict_order 不含 system
    assert memory_first.sections[SectionKey.SEMANTIC] > default.sections[SectionKey.SEMANTIC]
    assert SectionKey.SYSTEM not in memory_first.evict_order

    knowledge_first = load_budget("knowledge_first")
    assert knowledge_first.sections[SectionKey.RAG] > default.sections[SectionKey.RAG]


def test_load_budget_unknown_profile_raises() -> None:
    """未知 profile：fail fast 抛 BudgetConfigError。"""
    with pytest.raises(BudgetConfigError):
        load_budget("no-such-profile")


# ---- build 取数（真实 PG/Redis + fake 检索器） ----


class FakeRetriever:
    """检索假实现：返回预设结果，记录 query 供断言。"""

    def __init__(self, hits: list[ScoredMemory]) -> None:
        """Args: hits: retrieve 应返回的命中列表。"""
        self._hits = hits
        self.queries: list[str] = []

    async def retrieve(
        self,
        db: Any,
        *,
        ws_id: uuid.UUID,
        user_id: uuid.UUID,
        query: str,
        top_k: int | None = None,
    ) -> list[ScoredMemory]:
        """返回预设命中（签名与 MemoryRetriever.retrieve 一致）。"""
        self.queries.append(query)
        return self._hits


def _memory(key: str, content: str, memory_type: MemoryType) -> Memory:
    """构造未落库的 Memory ORM 对象（fake 检索结果载体）。"""
    return Memory(
        workspace_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        memory_type=memory_type,
        key=key,
        content=content,
    )


async def _prepare() -> tuple[Any, uuid.UUID, uuid.UUID, uuid.UUID]:
    """建 user/workspace/session 并写入滚动摘要，返回 (db, ws_id, user_id, session_id)。

    SessionService.create 返回 SessionRead（Pydantic），写 rolling_summary
    需经 repo 取 ORM 对象；内部异常时关闭 db 会话防连接泄漏
    （残留 idle in transaction 会阻塞后续用例的建表）。
    """
    db = get_session_factory()()
    try:
        user = await user_repo.create(
            db, username=f"ctx{uuid.uuid4().hex[:8]}", password_hash="x" * 60
        )
        ws = await workspace_repo.create(db, name="装配测试空间", owner_id=user.id)
        await workspace_repo.add_member(db, ws_id=ws.id, user_id=user.id, role=MemberRole.OWNER)
        created = await SessionService().create(db, ws_id=ws.id, user=user, title="装配测试")
        session = await session_repo.get_by_id(db, workspace_id=ws.id, session_id=created.id)
        assert session is not None
        session.rolling_summary = "用户此前讨论过向量检索方案。"
        await db.commit()
    except BaseException:
        await db.close()
        raise
    return db, ws.id, user.id, created.id


async def test_build_injects_summary_and_retrieved_memory() -> None:
    """build：摘要直取 + 检索命中分桶注入；同 key 摘要条目跳过防重复。"""
    db, ws_id, user_id, sid = await _prepare()
    try:
        wm = WorkingMemory(get_redis())
        await wm.append(sid, role="user", content="怎么配置检索？")

        retriever = FakeRetriever(
            [
                ScoredMemory(
                    _memory("profile.city", "用户住在杭州", MemoryType.SEMANTIC), 0.8, 0.7
                ),
                ScoredMemory(
                    _memory(
                        summary_memory_key(sid), "用户此前讨论过向量检索方案。", MemoryType.EPISODIC
                    ),
                    0.95,
                    0.9,
                ),
            ]
        )
        builder = ContextBuilder(wm, retriever)  # type: ignore[arg-type]
        result = await builder.build(
            db, ws_id=ws_id, user_id=user_id, session_id=sid, query="我家在哪座城市？"
        )

        assert retriever.queries == ["我家在哪座城市？"]
        system_msg = result.messages[0]["content"]
        # 检索命中按 memory_type 分桶注入
        assert '<memory type="semantic" source="profile.city">用户住在杭州</memory>' in system_msg
        # 本会话摘要直取（source=session:<sid>），检索同 key 条目不重复注入
        assert system_msg.count("用户此前讨论过向量检索方案。") == 1
        assert f'<summary source="session:{sid}">' in system_msg
        # working 末条为当前问题
        assert result.messages[-1] == {"role": "user", "content": "怎么配置检索？"}
    finally:
        await db.close()


async def test_build_without_retriever_degrades() -> None:
    """检索不可用（retriever=None）：降级为摘要 + 窗口，不抛异常。"""
    db, ws_id, user_id, sid = await _prepare()
    try:
        wm = WorkingMemory(get_redis())
        await wm.append(sid, role="user", content="继续刚才的话题")
        builder = ContextBuilder(wm, None)
        result = await builder.build(
            db, ws_id=ws_id, user_id=user_id, session_id=sid, query="继续刚才的话题"
        )
        assert f'<summary source="session:{sid}">' in result.messages[0]["content"]
        assert result.messages[-1]["content"] == "继续刚才的话题"
    finally:
        await db.close()


# ---- preview 端点 ----


async def _auth_setup(client: AsyncClient, username: str) -> tuple[dict, dict]:
    """注册登录并取个人 workspace，返回 (headers, ws)。"""
    await client.post("/api/auth/register", json={"username": username, "password": _PASSWORD})
    resp = await client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    ws = (await client.get("/api/workspaces", headers=headers)).json()[0]
    return headers, ws


async def test_preview_endpoint_roundtrip(client: AsyncClient) -> None:
    """preview：返回装配明细（区块用量 + messages），总 token 有界。"""
    headers, ws = await _auth_setup(client, "ctx_preview")
    created = (
        await client.post(
            f"/api/sessions?workspace_id={ws['id']}", headers=headers, json={"title": None}
        )
    ).json()
    # 直写 Working Memory（Redis 与 API 同进程共享），让 working 区块有数据
    await WorkingMemory(get_redis()).append(
        uuid.UUID(created["id"]), role="user", content="预览用近期消息"
    )

    resp = await client.post(
        "/api/context/preview",
        headers=headers,
        json={
            "workspace_id": ws["id"],
            "session_id": created["id"],
            "query": "用户的城市偏好是什么？",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["profile"] == "default"
    assert data["window_tokens"] == 4000
    assert data["available_tokens"] == 3600
    assert data["total_tokens"] <= data["available_tokens"]
    assert [s["key"] for s in data["sections"]] == [k.value for k in SectionKey]
    assert data["messages"][0]["role"] == "system"
    assert data["messages"][-1] == {"role": "user", "content": "预览用近期消息"}


async def test_preview_auth_and_validation(client: AsyncClient) -> None:
    """preview 防线：401 无令牌 / 404 会话不存在 / 422 非法 session_id / 403 他人 ws。"""
    headers, ws = await _auth_setup(client, "ctx_guard")
    other_headers, _ = await _auth_setup(client, "ctx_other")

    no_token = await client.post(
        "/api/context/preview",
        json={"workspace_id": ws["id"], "session_id": str(uuid.uuid4()), "query": "x"},
    )
    assert no_token.status_code == 401

    not_found = await client.post(
        "/api/context/preview",
        headers=headers,
        json={"workspace_id": ws["id"], "session_id": str(uuid.uuid4()), "query": "x"},
    )
    assert not_found.status_code == 404

    bad_uuid = await client.post(
        "/api/context/preview",
        headers=headers,
        json={"workspace_id": ws["id"], "session_id": "not-a-uuid", "query": "x"},
    )
    assert bad_uuid.status_code == 422

    cross = await client.post(
        "/api/context/preview",
        headers=other_headers,
        json={"workspace_id": ws["id"], "session_id": str(uuid.uuid4()), "query": "x"},
    )
    assert cross.status_code == 403
