"""FastAPI 依赖注入：数据库会话、Redis、认证与 workspace 隔离（docs/06 §M-01/M-02）。

隔离防线分工：
  第一道（repository 强制 workspace_id 过滤）→ 各仓储实现
  第二道（本模块 get_workspace / require_role 成员与角色校验）→ 路由声明使用
  第三道（向量库按 workspace 分区）→ M-04/M-07 实现
"""

import uuid
from collections.abc import AsyncIterator, Callable
from typing import Annotated

from fastapi import Depends, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError, PermissionDeniedError
from app.core.security import TokenError, decode_token
from app.db.session import session_scope
from app.models.enums import MemberRole
from app.models.user import User, Workspace, WorkspaceMember
from app.repositories import user_repo, workspace_repo

# 角色等级（数值越高权限越大；RBAC 比较基准）
_ROLE_ORDER: dict[MemberRole, int] = {
    MemberRole.MEMBER: 0,
    MemberRole.ADMIN: 1,
    MemberRole.OWNER: 2,
}


async def get_db() -> AsyncIterator[AsyncSession]:
    """请求级数据库会话依赖：正常提交、异常回滚。"""
    async for session in session_scope():
        yield session


DbDep = Annotated[AsyncSession, Depends(get_db)]

# ---- 认证 ----

_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    db: DbDep,
) -> User:
    """从 Bearer 令牌解析当前用户（隔离第二道防线的入口）。

    Raises:
        AppError: 401 —— 令牌缺失/无效/过期或用户不存在。
    """
    if credentials is None:
        raise AppError("unauthorized", 401, "缺少 Bearer 令牌")
    try:
        payload = decode_token(credentials.credentials, expected_type="access")
    except TokenError as exc:
        raise AppError("unauthorized", 401, str(exc)) from exc
    user = await user_repo.get_by_id(db, payload.user_id)
    if user is None or not user.is_active:
        raise AppError("unauthorized", 401, "用户不存在或已禁用")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_workspace(
    ws_id: uuid.UUID,
    user: CurrentUser,
    db: DbDep,
) -> Workspace:
    """校验当前用户是 workspace 成员后返回 workspace。

    Raises:
        PermissionDeniedError: 403 —— 非成员（workspace 不存在时同样 403，避免存在性泄露）。
    """
    member = await workspace_repo.get_member(db, ws_id=ws_id, user_id=user.id)
    if member is None:
        raise PermissionDeniedError()
    workspace = await workspace_repo.get_by_id(db, ws_id)
    if workspace is None:
        raise PermissionDeniedError()
    return workspace


CurrentWorkspace = Annotated[Workspace, Depends(get_workspace)]


async def get_workspace_from_query(
    workspace_id: Annotated[uuid.UUID, Query(description="目标 workspace ID")],
    user: CurrentUser,
    db: DbDep,
) -> Workspace:
    """query 参数版 workspace 校验（供 /api/sessions 等无路径前缀的路由）。

    Raises:
        PermissionDeniedError: 403 —— 非成员（含 workspace 不存在）。
    """
    member = await workspace_repo.get_member(db, ws_id=workspace_id, user_id=user.id)
    if member is None:
        raise PermissionDeniedError()
    workspace = await workspace_repo.get_by_id(db, workspace_id)
    if workspace is None:
        raise PermissionDeniedError()
    return workspace


CurrentWorkspaceQuery = Annotated[Workspace, Depends(get_workspace_from_query)]


def require_role(minimum: MemberRole) -> Callable[..., object]:
    """构造角色门槛依赖：返回 WorkspaceMember（已通过成员与角色双重校验）。

    Args:
        minimum: 允许访问的最低角色。

    Usage:
        member = Depends(require_role(MemberRole.ADMIN))
    """

    async def checker(
        ws_id: uuid.UUID,
        user: CurrentUser,
        db: DbDep,
    ) -> WorkspaceMember:
        member = await workspace_repo.get_member(db, ws_id=ws_id, user_id=user.id)
        if member is None:
            raise PermissionDeniedError()
        if _ROLE_ORDER[member.role] < _ROLE_ORDER[minimum]:
            raise PermissionDeniedError(f"需要 {minimum.value} 及以上角色")
        return member

    return checker


# ---- Redis ----

_redis_client: Redis | None = None


def get_redis() -> Redis:
    """返回进程级 Redis 客户端（异步，decode_responses=True）。"""
    global _redis_client
    if _redis_client is None:
        settings = get_settings()
        _redis_client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
    return _redis_client


async def close_redis() -> None:
    """关闭 Redis 连接（应用关闭/测试重置时调用；幂等）。"""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None
