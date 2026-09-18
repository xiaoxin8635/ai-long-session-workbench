"""认证安全原语：bcrypt 密码哈希与 JWT 签发/校验（docs/06 §M-02）。

设计要点：
  - bcrypt 自带随机盐，cost=12（约 250ms/次，暴力破解成本与可用性平衡）
  - 密码以 UTF-8 字节计不得超过 72（bcrypt 算法限制，schema 层前置校验）
  - access / refresh 双令牌，以 payload.type 区分，防止 refresh 冒充 access
"""
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.core.config import get_settings

# bcrypt 计算轮数（2^12 次；升级需评估登录延迟）
_BCRYPT_ROUNDS = 12


class TokenError(Exception):
    """令牌无效/过期/类型不符（由依赖层转 401）。"""


@dataclass(frozen=True)
class TokenPayload:
    """解码后的令牌载荷。"""

    user_id: uuid.UUID
    token_type: str  # "access" | "refresh"
    expires_at: datetime


# ---------- 密码 ----------


def hash_password(plain: str) -> str:
    """返回 bcrypt 哈希（含盐，格式 $2b$12$...）。

    Args:
        plain: 明文密码（UTF-8 字节数 ≤ 72，由 schema 保证）。
    """
    digest = bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS))
    return digest.decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文与哈希是否匹配；任何格式异常按不匹配处理。"""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))
    except ValueError:
        return False


# ---------- JWT ----------


def _create_token(user_id: uuid.UUID, token_type: str, lifetime: timedelta) -> str:
    """按类型与有效期签发 JWT。"""
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: uuid.UUID) -> str:
    """签发访问令牌（短效，默认 30 分钟）。"""
    settings = get_settings()
    return _create_token(user_id, "access", timedelta(minutes=settings.jwt_expire_minutes))


def create_refresh_token(user_id: uuid.UUID) -> str:
    """签发刷新令牌（长效，默认 7 天）。"""
    settings = get_settings()
    return _create_token(user_id, "refresh", timedelta(days=settings.refresh_expire_days))


def decode_token(token: str, expected_type: str) -> TokenPayload:
    """解码并校验令牌。

    Args:
        token: JWT 字符串。
        expected_type: 期望的令牌类型（access/refresh）。

    Returns:
        载荷（user_id/token_type/expires_at）。

    Raises:
        TokenError: 签名无效、已过期或类型不符。
    """
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise TokenError(f"令牌无效: {exc}") from exc
    if payload.get("type") != expected_type:
        raise TokenError("令牌类型不符")
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise TokenError("令牌载荷非法") from exc
    return TokenPayload(
        user_id=user_id,
        token_type=payload["type"],
        expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
    )
