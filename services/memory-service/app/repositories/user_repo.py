"""用户仓储（数据访问出口，禁止服务层直接写查询）。"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


async def create(
    db: AsyncSession,
    *,
    username: str,
    password_hash: str,
    email: str | None = None,
    display_name: str | None = None,
) -> User:
    """插入用户并返回（含数据库生成的主键/时间戳）。"""
    user = User(
        username=username,
        password_hash=password_hash,
        email=email,
        display_name=display_name or username,
    )
    db.add(user)
    await db.flush()  # 取回 id/created_at，事务由会话层统一提交
    await db.refresh(user)
    return user


async def get_by_username(db: AsyncSession, username: str) -> User | None:
    """按用户名查询（登录与注册去重用）。"""
    result = await db.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def get_by_id(db: AsyncSession, user_id: object) -> User | None:
    """按主键查询。"""
    return await db.get(User, user_id)
