"""Chat 服务：M1 直通编排（docs/06 §M-08 + M-04 后台抽取 + M-05 上下文装配）。

流程：解析/创建会话 → 持久化用户消息 → ContextBuilder 组装
（系统提示 + M-04 记忆检索 + 滚动摘要 + Working Memory 窗口，按区块预算裁剪）
→ LLM 流式生成 → 持久化回答 + 回写窗口 → 触发滚动摘要压缩（M-06）
→ 后台派发记忆抽取任务（M-04，不阻塞响应）。

M3 阶段此编排将升级为 LangGraph 图（retrieve→assemble→generate→tools），
当前保持直通以保证 Open WebUI 先跑通完整对话。
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.context.builder import ContextBuilder, default_builder
from app.core.errors import NotFoundError
from app.llm.client import LLMClient
from app.memory.pipeline import extract_pipeline
from app.memory.working_memory import WorkingMemory
from app.models.user import User
from app.repositories import session_repo
from app.schemas.chat import ChatCompletionRequest
from app.schemas.session import MessageCreate
from app.services.session_service import SessionService
from app.summarizer.rolling import RollingSummarizer

logger = logging.getLogger(__name__)

# 会话默认标题：取首条用户消息截断
_TITLE_MAX = 30

_session_service = SessionService()

# 后台抽取任务引用池：防止 asyncio.create_task 的任务被 GC 中途取消
_background_tasks: set[asyncio.Task[object]] = set()


def _spawn_extraction(
    ws_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    messages: Sequence[tuple[uuid.UUID, str, str]],
) -> None:
    """派发后台记忆抽取任务（fire-and-forget，管线内部吞异常）。"""
    task = asyncio.create_task(
        extract_pipeline(
            ws_id=ws_id, user_id=user_id, session_id=session_id, messages=list(messages)
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


class ChatService:
    """chat/completions 用例编排（流式与非流式共用取上下文与落库逻辑）。"""

    def __init__(
        self,
        llm: LLMClient,
        working_memory: WorkingMemory,
        summarizer: RollingSummarizer | None = None,
        extract_hook: Callable[..., Awaitable[object]] | None = _spawn_extraction,
        context_builder: ContextBuilder | None = None,
    ) -> None:
        """注入 LLM 客户端、Working Memory、滚动摘要器、抽取钩子与上下文装配器。

        Args:
            llm: 对话 LLM 客户端。
            working_memory: Working Memory 窗口。
            summarizer: 滚动摘要器；None 时用 llm+working_memory 默认构造。
            extract_hook: 每轮回答后的记忆抽取派发函数（签名同 _spawn_extraction）；
                None 禁用（测试隔离用）。
            context_builder: 上下文装配器（M-05）；None 时基于 working_memory
                惰性构造（embedding 未配置则检索降级）。
        """
        self._llm = llm
        self._wm = working_memory
        self._summarizer = summarizer or RollingSummarizer(llm, working_memory)
        self._extract_hook = extract_hook
        self._context_builder = context_builder or default_builder(working_memory)

    async def resolve_session(
        self, db: AsyncSession, *, ws_id: uuid.UUID, user: User, payload: ChatCompletionRequest
    ) -> uuid.UUID:
        """取请求指向的会话；无 session_id 时自动创建（标题取首条用户消息）。"""
        if payload.metadata.session_id:
            session = await session_repo.get_by_id(
                db,
                workspace_id=ws_id,
                session_id=uuid.UUID(payload.metadata.session_id),
            )
            if session is None:
                raise NotFoundError("会话不存在")
            return session.id
        first_user = next((m.content for m in payload.messages if m.role == "user"), "新会话")
        created = await _session_service.create(
            db, ws_id=ws_id, user=user, title=first_user[:_TITLE_MAX]
        )
        return created.id

    async def prepare(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        session_id: uuid.UUID,
        payload: ChatCompletionRequest,
    ) -> uuid.UUID:
        """持久化本轮用户消息并回写 Working Memory（生成前执行）。

        Returns:
            用户消息的数据库 ID（供记忆抽取的 source_message_ids）。
        """
        user_msg = payload.messages[-1]
        message = await _session_service.append_message(
            db,
            ws_id=ws_id,
            session_id=session_id,
            payload=MessageCreate(role=user_msg.role, content=user_msg.content),
        )
        await self._wm.append(session_id, role=user_msg.role, content=user_msg.content)
        return message.id

    async def context_messages(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        query: str,
    ) -> list[dict[str, str]]:
        """组装 LLM 输入（M-05 ContextBuilder 全权负责）。

        system（含记忆标签/摘要/知识注入）+ Working Memory 窗口，
        按区块预算裁剪；当前用户问题作为长期记忆检索的 query。
        """
        assembled = await self._context_builder.build(
            db, ws_id=ws_id, user_id=user_id, session_id=session_id, query=query
        )
        return assembled.messages

    async def finalize(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        answer: str,
        user_msg_id: uuid.UUID,
        user_content: str,
    ) -> None:
        """持久化回答并回写 Working Memory，随后触发压缩与后台抽取。

        压缩在 maybe_compress 内部自持会话锁并吞异常；抽取为后台任务
        fire-and-forget —— 两者失败均不影响对话。

        Args:
            db: 数据库会话。
            ws_id: 所属 workspace。
            user_id: 归属用户（记忆条目归属）。
            session_id: 会话。
            answer: 完整回答文本。
            user_msg_id: 本轮用户消息的数据库 ID（prepare 的返回值）。
            user_content: 本轮用户消息正文（抽取管线的输入）。
        """
        message = await _session_service.append_message(
            db,
            ws_id=ws_id,
            session_id=session_id,
            payload=MessageCreate(role="assistant", content=answer),
        )
        await self._wm.append(session_id, role="assistant", content=answer)
        await self._summarizer.maybe_compress(db, ws_id=ws_id, session_id=session_id)
        if self._extract_hook is not None:
            self._extract_hook(
                ws_id,
                user_id,
                session_id,
                [
                    (user_msg_id, "user", user_content),
                    (message.id, "assistant", answer),
                ],
            )

    async def stream_answer(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        payload: ChatCompletionRequest,
        user_msg_id: uuid.UUID,
    ) -> AsyncIterator[str]:
        """流式生成并逐段产出文本；结束后统一落库并触发压缩/抽取。"""
        messages = await self.context_messages(
            db,
            ws_id=ws_id,
            user_id=user_id,
            session_id=session_id,
            query=payload.messages[-1].content,
        )
        chunks: list[str] = []
        async for delta in self._llm.stream_chat(messages):
            chunks.append(delta)
            yield delta
        await self.finalize(
            db,
            ws_id=ws_id,
            user_id=user_id,
            session_id=session_id,
            answer="".join(chunks),
            user_msg_id=user_msg_id,
            user_content=payload.messages[-1].content,
        )

    async def complete_answer(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        payload: ChatCompletionRequest,
        user_msg_id: uuid.UUID,
    ) -> str:
        """非流式生成；返回完整回答（落库在内部完成）。"""
        messages = await self.context_messages(
            db,
            ws_id=ws_id,
            user_id=user_id,
            session_id=session_id,
            query=payload.messages[-1].content,
        )
        answer, _usage = await self._llm.complete(messages)
        await self.finalize(
            db,
            ws_id=ws_id,
            user_id=user_id,
            session_id=session_id,
            answer=answer,
            user_msg_id=user_msg_id,
            user_content=payload.messages[-1].content,
        )
        return answer
