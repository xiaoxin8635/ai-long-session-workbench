"""认证相关 schema（docs/06 §M-02 / §2.4 命名约定）。"""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field

# 密码长度限制：bcrypt 以字节计上限 72；下限 8 保证基本强度
_PASSWORD_FIELD = Field(min_length=8, max_length=72, description="密码（8-72 字符）")

# 简化邮箱校验（避免引入 email-validator 依赖；域名部分宽松匹配）
_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class UserCreate(BaseModel):
    """注册请求。"""

    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    password: str = _PASSWORD_FIELD
    email: str | None = Field(default=None, max_length=255, pattern=_EMAIL_PATTERN)
    display_name: str | None = Field(default=None, max_length=128)


class LoginRequest(BaseModel):
    """登录请求（JSON 形式；与 OAuth2 password flow 等价）。"""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=72)


class RefreshRequest(BaseModel):
    """刷新请求。"""

    refresh_token: str = Field(min_length=1)


class UserRead(BaseModel):
    """用户响应。"""

    id: uuid.UUID
    username: str
    email: str | None
    display_name: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class TokenPair(BaseModel):
    """登录/刷新响应的双令牌。"""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
