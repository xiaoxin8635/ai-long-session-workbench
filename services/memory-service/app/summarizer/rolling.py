"""滚动摘要（docs/06 §M-06）：单会话上下文压缩。

机制：
  - 触发：对话消息追加后调用 maybe_compress；Working Memory 全量与
    token 预算窗口的差集即"滑出段"，滑出段非空才继续（缓存命中零成本）
  - 压缩：旧摘要 + 滑出段 → LLM 增量合并为新摘要；摘要自身超预算时
    二级压缩（防摘要无限膨胀）
  - 衔接：摘要落 sessions.rolling_summary 后 LTRIM 窗口只留预算内消息，
    下轮上下文 = 摘要(system) + 窗口（不重不漏）
  - 沉淀：同步 upsert episodic 记忆条目（会话结束/后续检索可用）
"""

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_redis
from app.core.errors import ConflictError
from app.llm.client import LLMClient
from app.memory.working_memory import WorkingMemory
from app.models.enums import SessionStatus
from app.repositories import memory_repo, session_repo
from app.services.session_service import SessionLock
from app.services.token_counter import count_tokens
from app.summarizer.prompts import build_compress_messages, build_incremental_messages

logger = logging.getLogger(__name__)


class RollingSummarizer:
    """会话滚动摘要器：窗口滑出段增量并入摘要并沉淀 episodic 记忆。"""

    def __init__(self, llm: LLMClient, working_memory: WorkingMemory) -> None:
        """注入 LLM 客户端与 Working Memory（测试可替换为 fake）。"""
        self._llm = llm
        self._wm = working_memory

    async def maybe_compress(
        self, db: AsyncSession, *, ws_id: uuid.UUID, session_id: uuid.UUID
    ) -> bool:
        """尝试压缩会话上下文；滑出段为空时零成本返回 False。

        全程持有会话分布式锁（与消息追加互斥）。锁竞争（对方追加消息中）
        时放弃本轮压缩（下一轮对话会再触发）；其余异常降级记日志，
        均不影响主对话。

        Args:
            db: 数据库会话。
            ws_id: 所属 workspace（隔离校验）。
            session_id: 目标会话。

        Returns:
            是否实际执行了压缩（False = 缓存命中/锁竞争/降级）。
        """
        settings = get_settings()
        lock = SessionLock(get_redis(), session_id)
        try:
            async with lock.hold():
                return await self._compress_locked(
                    db,
                    ws_id=ws_id,
                    session_id=session_id,
                    window_tokens=settings.context_window_tokens,
                    summary_max_tokens=settings.rolling_summary_max_tokens,
                )
        except ConflictError:
            # 追加消息持锁中：放弃本轮，下轮对话再压（不视为错误）
            logger.info("rolling_compress_skipped_lock_busy session=%s", session_id)
            return False
        except Exception:
            logger.exception("rolling_compress_failed ws=%s session=%s", ws_id, session_id)
            return False

    async def _compress_locked(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        session_id: uuid.UUID,
        window_tokens: int,
        summary_max_tokens: int,
    ) -> bool:
        """锁内压缩主体（调用方已持有会话锁与异常兜底）。"""
        session = await session_repo.get_by_id(db, workspace_id=ws_id, session_id=session_id)
        if session is None or session.status != SessionStatus.ACTIVE:
            return False

        all_entries = await self._wm.all_entries(session_id)
        window = await self._wm.window(session_id, max_tokens=window_tokens)
        evicted = all_entries[: len(all_entries) - len(window)] if window else all_entries[:-1]
        if not evicted:
            return False  # 缓存命中：无滑出段，不调模型

        # ---- 增量合并：旧摘要 + 滑出段 → 新摘要 ----
        merge_messages = build_incremental_messages(
            session.rolling_summary,
            [{"role": e["role"], "content": e["content"]} for e in evicted],
        )
        new_summary, _usage = await self._llm.complete(merge_messages)

        # ---- 二级压缩：摘要自身超预算时再压（一次，仍超则截断保底）----
        if count_tokens(new_summary) > summary_max_tokens:
            compressed, _ = await self._llm.complete(build_compress_messages(new_summary))
            new_summary = compressed
            if count_tokens(new_summary) > summary_max_tokens:
                logger.warning(
                    "rolling_summary_still_over_budget session=%s tokens=%s",
                    session_id,
                    count_tokens(new_summary),
                )

        # ---- 落库 + 窗口衔接 + episodic 沉淀（锁内提交）----
        session.rolling_summary = new_summary
        await memory_repo.upsert_episodic_summary(
            db, ws_id=ws_id, session_id=session_id, user_id=session.user_id, content=new_summary
        )
        await self._wm.trim_to_keep(session_id, keep=len(window))
        await db.commit()
        logger.info(
            "rolling_compressed session=%s evicted=%s summary_tokens=%s",
            session_id,
            len(evicted),
            count_tokens(new_summary),
        )
        return True
