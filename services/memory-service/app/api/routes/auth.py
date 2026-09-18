"""认证路由：注册 / 登录 / 令牌刷新（docs/06 §M-02）。

注册为单事务：用户 → 个人 workspace → owner 成员关系，三者原子生效；
登录失败统一 401（防用户枚举）。
"""

from fastapi import APIRouter, status
from sqlalchemy.exc import IntegrityError

from app.core.deps import CurrentUser, DbDep
from app.core.errors import AppError, ConflictError
from app.core.security import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.enums import MemberRole
from app.repositories import user_repo, workspace_repo
from app.schemas.auth import LoginRequest, RefreshRequest, TokenPair, UserCreate, UserRead

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 统一登录失败文案（不区分"用户不存在"与"密码错误"）
_LOGIN_FAILED = "用户名或密码错误"


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="注册用户（自动创建个人 workspace 并绑 owner）",
)
async def register(payload: UserCreate, db: DbDep) -> UserRead:
    """注册新用户；同名冲突返回 409。

    单事务完成：users → workspaces（"{username} 的空间"）→ workspace_members(owner)。
    """
    if await user_repo.get_by_username(db, payload.username) is not None:
        raise ConflictError("用户名已被占用")
    try:
        user = await user_repo.create(
            db,
            username=payload.username,
            password_hash=hash_password(payload.password),
            email=payload.email,
            display_name=payload.display_name,
        )
        ws = await workspace_repo.create(
            db, name=f"{payload.display_name or payload.username} 的空间", owner_id=user.id
        )
        await workspace_repo.add_member(db, ws_id=ws.id, user_id=user.id, role=MemberRole.OWNER)
    except IntegrityError as exc:  # 并发注册同名用户
        raise ConflictError("用户名已被占用") from exc
    return UserRead.model_validate(user)


@router.post("/login", response_model=TokenPair, summary="登录获取双令牌")
async def login(payload: LoginRequest, db: DbDep) -> TokenPair:
    """用户名 + 密码换取 access(30min) / refresh(7d) 双令牌。

    任何失败（用户不存在 / 密码错误 / 用户禁用）统一 401，防枚举。
    """
    user = await user_repo.get_by_username(db, payload.username)
    if user is None or not verify_password(payload.password, user.password_hash):
        raise AppError("invalid_credentials", 401, _LOGIN_FAILED)
    if not user.is_active:
        raise AppError("invalid_credentials", 401, _LOGIN_FAILED)
    return TokenPair(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
    )


@router.post("/refresh", response_model=TokenPair, summary="用 refresh 令牌换新双令牌")
async def refresh(payload: RefreshRequest, db: DbDep) -> TokenPair:
    """校验 refresh 类型令牌后签发新令牌对（简单轮换：旧 refresh 即刻失效于过期）。"""
    try:
        token = decode_token(payload.refresh_token, expected_type="refresh")
    except TokenError as exc:
        raise AppError("unauthorized", 401, str(exc)) from exc
    user = await user_repo.get_by_id(db, token.user_id)
    if user is None or not user.is_active:
        raise AppError("unauthorized", 401, "用户不存在或已禁用")
    return TokenPair(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
    )


@router.get("/me", response_model=UserRead, summary="获取当前登录用户信息")
async def me(user: CurrentUser) -> UserRead:
    """Bearer access 令牌自省端点（前端会话恢复用）。"""
    return UserRead.model_validate(user)
