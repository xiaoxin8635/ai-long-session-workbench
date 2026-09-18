"""M-06 滚动摘要测试（Fake LLM + 真实 PG/Redis，服务层直测，docs/06 §8）。

覆盖：
  - 滑出段增量并入：rolling_summary 落库 + 窗口 LTRIM 衔接 + episodic 沉淀
  - 缓存命中零调用（窗口内无滑出段时不调模型）
  - 摘要超预算二级压缩
  - episodic upsert 幂等（version 自增，不重复建条目）
  - 100 轮长对话上下文 token 有界（DoD 稳定性验收）
"""

import uuid

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.deps import get_redis
from app.db.session import get_session_factory
from app.llm.client import LLMUsage
from app.memory.working_memory import WorkingMemory
from app.models.enums import (
    MemberRole,
    MemoryEventType,
    MemoryStatus,
    MemoryType,
)
from app.models.memory import Memory, MemoryEvent
from app.repositories import session_repo, user_repo, workspace_repo
from app.repositories.memory_repo import summary_memory_key
from app.services.chat_service import ChatService
from app.services.session_service import SessionService
from app.services.token_counter import count_tokens
from app.summarizer.rolling import RollingSummarizer

# 小预算测试口径：窗口 40 token（约 2 条 15 字中文消息），
# 摘要上限 50 token（构造超限摘要触发二级压缩）
_WINDOW_TOKENS = 40
_SUMMARY_MAX_TOKENS = 50
# 100 轮有界性验收的上下文总 token 上界：摘要(≤50) + 窗口(≤40) + 摘要前缀等余量
_CONTEXT_TOKEN_CEILING = 150


class FakeSummaryLLM:
    """可编程摘要模型：按序返回预设回复并记录每次调用输入。

    回复列表耗尽后一直复用最后一个（100 轮场景无需罗列百余个预设）。
    """

    def __init__(self, replies: list[str]) -> None:
        """Args: replies: 按调用次序弹出的预设回复。"""
        self._replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]]) -> tuple[str, LLMUsage]:
        """非流式补全：记录输入并返回预设回复（token 计数与断言无关，置 0）。"""
        self.calls.append(messages)
        reply = self._replies[min(len(self.calls) - 1, len(self._replies) - 1)]
        return reply, LLMUsage(prompt_tokens=0, completion_tokens=0)


@pytest.fixture
def small_budget(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """注入小预算配置（避免真实 4000 token 窗口需要海量测试消息）。

    同时 patch rolling 与 chat_service 两处 get_settings 引用，
    保证压缩触发与上下文组装使用同一套预算口径。
    """
    settings = Settings(
        context_window_tokens=_WINDOW_TOKENS,
        rolling_summary_max_tokens=_SUMMARY_MAX_TOKENS,
    )
    monkeypatch.setattr("app.summarizer.rolling.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.chat_service.get_settings", lambda: settings)
    return settings


async def _prepare_session() -> tuple[object, uuid.UUID, uuid.UUID]:
    """创建 user/workspace/session，返回 (db, ws_id, session_id)。

    调用方负责在使用结束后 close 返回的 db 会话。
    """
    db = get_session_factory()()
    user = await user_repo.create(db, username=f"sum{uuid.uuid4().hex[:8]}", password_hash="x" * 60)
    ws = await workspace_repo.create(db, name="摘要测试空间", owner_id=user.id)
    await workspace_repo.add_member(db, ws_id=ws.id, user_id=user.id, role=MemberRole.OWNER)
    session = await SessionService().create(db, ws_id=ws.id, user=user, title="摘要测试")
    await db.commit()
    return db, ws.id, session.id


def _build_service(llm: FakeSummaryLLM) -> tuple[WorkingMemory, RollingSummarizer, ChatService]:
    """构造 WorkingMemory + RollingSummarizer + ChatService（共用同一 fake LLM）。"""
    wm = WorkingMemory(get_redis())
    summarizer = RollingSummarizer(llm, wm)  # type: ignore[arg-type]
    chat = ChatService(llm, wm, summarizer=summarizer)  # type: ignore[arg-type]
    return wm, summarizer, chat


async def _memories_of(db: object, ws_id: uuid.UUID) -> list[Memory]:
    """查询 workspace 下全部记忆条目（断言 episodic 沉淀用）。"""
    result = await db.execute(select(Memory).where(Memory.workspace_id == ws_id))
    return list(result.scalars().all())


async def test_compress_evicts_and_persists(small_budget: Settings) -> None:
    """滑出段并入摘要：rolling_summary 落库、窗口裁剪、episodic 条目出现。"""
    db, ws_id, sid = await _prepare_session()
    try:
        wm, summarizer, _ = _build_service(FakeSummaryLLM(["用户与助手讨论了记忆压缩机制。"]))
        for i in range(4):  # 4 条 × 20 token = 80 token，窗口 40 只装得下 2 条
            await wm.append(sid, role="user" if i % 2 == 0 else "assistant", content="忆" * 20)

        assert await summarizer.maybe_compress(db, ws_id=ws_id, session_id=sid) is True

        session = await session_repo.get_by_id(db, workspace_id=ws_id, session_id=sid)
        assert session is not None
        assert session.rolling_summary == "用户与助手讨论了记忆压缩机制。"

        # 增量合并 prompt：首次压缩旧摘要为"（暂无）"，滑出段原文进入 user 消息
        assert len(summarizer._llm.calls) == 1  # noqa: SLF001 —— 直测内部记录
        system_prompt = summarizer._llm.calls[0][0]["content"]  # noqa: SLF001
        assert "（暂无）" in system_prompt
        assert "忆" in summarizer._llm.calls[0][1]["content"]  # noqa: SLF001

        # 窗口 LTRIM：只剩预算内的 2 条（摘要与窗口衔接，不重不漏）
        assert len(await wm.all_entries(sid)) == 2

        # episodic 沉淀：单条 ACTIVE 条目，key/source_session 指向本会话
        memories = await _memories_of(db, ws_id)
        assert len(memories) == 1
        mem = memories[0]
        assert mem.memory_type == MemoryType.EPISODIC
        assert mem.key == summary_memory_key(sid)
        assert mem.source_session_id == sid
        assert mem.status == MemoryStatus.ACTIVE
        assert mem.version == 1
    finally:
        await db.close()


async def test_cache_hit_skips_llm(small_budget: Settings) -> None:
    """窗口内无滑出段（缓存命中）：返回 False 且零 LLM 调用。"""
    db, ws_id, sid = await _prepare_session()
    try:
        wm, summarizer, _ = _build_service(FakeSummaryLLM(["不应被调用"]))
        await wm.append(sid, role="user", content="你好")  # 单条远小于预算

        assert await summarizer.maybe_compress(db, ws_id=ws_id, session_id=sid) is False

        assert summarizer._llm.calls == []  # noqa: SLF001
        session = await session_repo.get_by_id(db, workspace_id=ws_id, session_id=sid)
        assert session is not None
        assert session.rolling_summary is None
    finally:
        await db.close()


async def test_second_level_compress(small_budget: Settings) -> None:
    """摘要超出预算（50 token）时触发二级压缩，最终保存压缩版。"""
    db, ws_id, sid = await _prepare_session()
    try:
        over_budget_summary = "压" * 100  # 100 token > 50
        compressed_summary = "压缩后的短摘要"
        wm, summarizer, _ = _build_service(
            FakeSummaryLLM([over_budget_summary, compressed_summary])
        )
        for i in range(4):
            await wm.append(sid, role="user" if i % 2 == 0 else "assistant", content="忆" * 20)

        assert await summarizer.maybe_compress(db, ws_id=ws_id, session_id=sid) is True

        # 第一次增量合并返回超限摘要 → 第二次二级压缩输入即该摘要原文
        assert len(summarizer._llm.calls) == 2  # noqa: SLF001
        assert summarizer._llm.calls[1][1]["content"] == over_budget_summary  # noqa: SLF001
        session = await session_repo.get_by_id(db, workspace_id=ws_id, session_id=sid)
        assert session is not None
        assert session.rolling_summary == compressed_summary
    finally:
        await db.close()


async def test_episodic_upsert_idempotent(small_budget: Settings) -> None:
    """二次压缩走 upsert：同 key 条目 version+1，事件流水 CREATED→UPDATED。"""
    db, ws_id, sid = await _prepare_session()
    try:
        wm, summarizer, _ = _build_service(FakeSummaryLLM(["摘要版本一", "摘要版本二"]))
        for i in range(4):
            await wm.append(sid, role="user" if i % 2 == 0 else "assistant", content="忆" * 20)
        assert await summarizer.maybe_compress(db, ws_id=ws_id, session_id=sid) is True

        # 追加新消息使再次滑出（trim 后剩 2 条 + 新 2 条 = 4 条）
        for i in range(2):
            await wm.append(sid, role="user" if i % 2 == 0 else "assistant", content="新" * 20)
        assert await summarizer.maybe_compress(db, ws_id=ws_id, session_id=sid) is True

        memories = await _memories_of(db, ws_id)
        assert len(memories) == 1  # 幂等：不重复建条目
        mem = memories[0]
        assert mem.version == 2
        assert mem.content == "摘要版本二"

        events = (
            (
                await db.execute(
                    select(MemoryEvent)
                    .where(MemoryEvent.memory_id == mem.id)
                    .order_by(MemoryEvent.created_at)
                )
            )
            .scalars()
            .all()
        )
        assert [e.event_type for e in events] == [
            MemoryEventType.CREATED,
            MemoryEventType.UPDATED,
        ]
    finally:
        await db.close()


async def test_hundred_rounds_context_bounded(small_budget: Settings) -> None:
    """DoD 验收：100 轮长对话后每轮上下文 token 有界且摘要持续沉淀。"""
    db, ws_id, sid = await _prepare_session()
    try:
        final_summary = "用户在长对话中持续讨论记忆压缩与上下文预算。"
        wm, summarizer, chat = _build_service(FakeSummaryLLM([final_summary]))
        for i in range(100):
            await wm.append(sid, role="user" if i % 2 == 0 else "assistant", content="话" * 15)
            await summarizer.maybe_compress(db, ws_id=ws_id, session_id=sid)
            messages = await chat.context_messages(db, ws_id=ws_id, session_id=sid)
            total = sum(count_tokens(m["content"]) for m in messages)
            assert total <= _CONTEXT_TOKEN_CEILING, f"第 {i} 轮上下文超界: {total}"

        session = await session_repo.get_by_id(db, workspace_id=ws_id, session_id=sid)
        assert session is not None
        assert session.rolling_summary == final_summary

        # 摘要以 system 前缀注入窗口之前（首条即摘要）
        messages = await chat.context_messages(db, ws_id=ws_id, session_id=sid)
        assert messages[0]["content"].startswith("[会话早期内容摘要]")

        # 压缩确实反复触发（version 自增），episodic 条目仍唯一
        memories = await _memories_of(db, ws_id)
        assert len(memories) == 1
        assert memories[0].version >= 2
        assert summarizer._llm.calls  # noqa: SLF001
    finally:
        await db.close()
