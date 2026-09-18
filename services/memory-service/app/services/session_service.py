"""会话服务：CRUD 编排、Redis 会话锁、乐观锁与级联归档（docs/06 §M-03）。

数据流：POST message → 会话锁 → token 计数 → INSERT → 原子更新
sessions.last_message_at/token_total/version → 版本冲突转 409。
"""

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from uuid import uuid4

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_redis
from app.core.errors import ConflictError, NotFoundError
from app.models.enums import SessionStatus
from app.models.session import Session
from app.models.user import User
from app.repositories import memory_repo, message_repo, session_repo
from app.schemas.message import MessageRead
from app.schemas.session import MessageCreate, SessionDetail, SessionRead
from app.services.token_counter import count_tokens

# 会话默认标题（首条消息后由摘要命名，M-06 接管）
_DEFAULT_TITLE = "新会话"


class SessionLock:
    """Redis 分布式锁：同会话写操作串行化（SET NX PX + Lua 原子释放）。

    防误删：释放时校验持有令牌，只删除自己加的锁；
    超时自愈：锁自带 TTL，持锁方崩溃后锁自动过期。
    """

    _RELEASE_LUA = (
        "if redis.call('get', KEYS[1]) == ARGV[1] "
        "then return redis.call('del', KEYS[1]) else return 0 end"
    )

    def __init__(
        self,
        redis: Redis,
        session_id: uuid.UUID,
        *,
        ttl_seconds: float = 5.0,
        acquire_timeout: float = 3.0,
        retry_interval: float = 0.05,
    ) -> None:
        """初始化锁参数。

        Args:
            redis: Redis 客户端（复用进程级连接）。
            session_id: 目标会话。
            ttl_seconds: 锁 TTL（持锁方崩溃的自愈上限）。
            acquire_timeout: 获取锁的最长等待，超时视为并发冲突（409）。
            retry_interval: 获取重试间隔（秒）。
        """
        self._redis = redis
        self._key = f"lock:session:{session_id}"
        self._token = uuid4().hex
        self._ttl_ms = int(ttl_seconds * 1000)
        self._acquire_timeout = acquire_timeout
        self._retry_interval = retry_interval

    async def acquire(self) -> None:
        """获取锁；超时抛 ConflictError（并发写入冲突）。"""
        deadline = time.monotonic() + self._acquire_timeout
        while True:
            ok = await self._redis.set(self._key, self._token, nx=True, px=self._ttl_ms)
            if ok:
                return
            if time.monotonic() >= deadline:
                raise ConflictError("会话并发写入冲突，请重试")
            await asyncio.sleep(self._retry_interval)

    async def release(self) -> None:
        """释放锁（令牌校验，原子；非持有者删除无效）。"""
        await self._redis.eval(self._RELEASE_LUA, 1, self._key, self._token)

    @asynccontextmanager
    async def hold(self):
        """上下文管理器形式：进入获取、退出释放（异常也保证释放）。"""
        await self.acquire()
        try:
            yield
        finally:
            await self.release()


class SessionService:
    """会话用例编排（路由薄、服务厚；DB 会话由依赖注入传入）。"""

    async def create(
        self, db: AsyncSession, *, ws_id: uuid.UUID, user: User, title: str | None
    ) -> SessionRead:
        """创建会话；title 为空时用占位，首条消息后由摘要命名。"""
        session = await session_repo.create(
            db,
            workspace_id=ws_id,
            user_id=user.id,
            title=title or _DEFAULT_TITLE,
        )
        return SessionRead.model_validate(session)

    async def list_paginated(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        limit: int,
        offset: int,
        keyword: str | None = None,
        include_archived: bool = False,
    ) -> tuple[list[SessionRead], int]:
        """分页列出会话（默认排除软删除与归档）。"""
        sessions, total = await session_repo.list_paginated(
            db,
            workspace_id=ws_id,
            limit=limit,
            offset=offset,
            keyword=keyword,
            include_archived=include_archived,
        )
        return [SessionRead.model_validate(s) for s in sessions], total

    async def get(self, db: AsyncSession, *, ws_id: uuid.UUID, session_id: uuid.UUID) -> Session:
        """取会话（含已归档；不存在/跨 workspace → 404）。"""
        session = await session_repo.get_by_id(db, workspace_id=ws_id, session_id=session_id)
        if session is None or session.status == SessionStatus.DELETED:
            raise NotFoundError("会话不存在")
        return session

    async def rename(
        self, db: AsyncSession, *, ws_id: uuid.UUID, session_id: uuid.UUID, title: str
    ) -> SessionRead:
        """重命名会话。"""
        session = await self.get(db, ws_id=ws_id, session_id=session_id)
        session.title = title
        await db.flush()
        await db.refresh(session)
        return SessionRead.model_validate(session)

    async def archive(
        self, db: AsyncSession, *, ws_id: uuid.UUID, session_id: uuid.UUID
    ) -> SessionRead:
        """归档会话（可逆：status 往返 active/archived）。"""
        session = await self.get(db, ws_id=ws_id, session_id=session_id)
        session.status = SessionStatus.ARCHIVED
        await db.flush()
        await db.refresh(session)
        return SessionRead.model_validate(session)

    async def restore(
        self, db: AsyncSession, *, ws_id: uuid.UUID, session_id: uuid.UUID
    ) -> SessionRead:
        """恢复归档会话。"""
        session = await self.get(db, ws_id=ws_id, session_id=session_id)
        session.status = SessionStatus.ACTIVE
        await db.flush()
        await db.refresh(session)
        return SessionRead.model_validate(session)

    async def delete(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        session_id: uuid.UUID,
        cascade_memories: bool = False,
    ) -> int:
        """软删除会话；cascade_memories=True 时同步归档来源记忆。

        Returns:
            级联归档的记忆条数（cascade 关闭时为 0）。
        """
        session = await self.get(db, ws_id=ws_id, session_id=session_id)
        archived = 0
        if cascade_memories:
            archived = await memory_repo.archive_by_source_session(db, session_id=session.id)
        await session_repo.soft_delete(db, session)
        return archived

    async def list_messages(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        session_id: uuid.UUID,
        limit: int,
        offset: int,
    ) -> tuple[list[MessageRead], int]:
        """会话消息分页（时间正序）。"""
        session = await self.get(db, ws_id=ws_id, session_id=session_id)
        messages, total = await message_repo.list_by_session(
            db, session_id=session.id, limit=limit, offset=offset
        )
        return [MessageRead.from_message(m) for m in messages], total

    async def get_detail(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        session_id: uuid.UUID,
        limit: int = 20,
    ) -> SessionDetail:
        """会话详情（附首页消息）。"""
        session = await self.get(db, ws_id=ws_id, session_id=session_id)
        messages, _total = await message_repo.list_by_session(
            db, session_id=session.id, limit=limit, offset=0
        )
        detail = SessionDetail.model_validate(session)
        detail.messages = [MessageRead.from_message(m) for m in messages]
        return detail

    async def append_message(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        session_id: uuid.UUID,
        payload: MessageCreate,
    ) -> MessageRead:
        """追加消息（锁 + 乐观锁双保险的写路径）。

        流程：会话锁 → 期望版本校验 → token 计数 → INSERT 消息 →
        原子更新会话计数（WHERE version 校验）→ 锁内提交 → 冲突转 409。

        关键点：必须在锁内显式 commit —— get_db 依赖的统一提交发生在
        handler 返回之后（锁已释放），若不在锁内提交，并发请求会在
        未提交数据上读到旧版本，造成虚假 409。

        Raises:
            NotFoundError: 404 —— 会话不存在或已删除。
            ConflictError: 409 —— 会话非 active / 版本冲突 / 锁获取超时。
        """
        lock = SessionLock(get_redis(), session_id)
        async with lock.hold():
            session = await self.get(db, ws_id=ws_id, session_id=session_id)
            if session.status != SessionStatus.ACTIVE:
                raise ConflictError("会话已归档，不能追加消息")
            if payload.expected_version is not None and (
                payload.expected_version != session.version
            ):
                raise ConflictError("会话版本冲突，请刷新后重试")

            message = await message_repo.create(
                db,
                session_id=session.id,
                role=payload.role,
                content=payload.content,
                token_count=count_tokens(payload.content),
                metadata_=payload.metadata,
            )
            updated = await session_repo.bump_counters(
                db,
                session_id=session.id,
                expected_version=session.version,
                add_tokens=message.token_count,
            )
            if not updated:
                raise ConflictError("会话版本冲突，请刷新后重试")
            result = MessageRead.from_message(message)
            await db.commit()  # 锁内提交（见 docstring 关键点）
            return result
