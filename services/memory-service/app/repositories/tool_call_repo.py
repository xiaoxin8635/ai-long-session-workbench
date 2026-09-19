"""工具调用审计仓储（M-09，docs/01 §4.1 tool_call_logs）。

全量调用（成功/失败/拒绝/超时）都落一条审计记录；external 风险的
pending 记录由确认流推进至终态。本仓储只管数据形态，状态机在执行器。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ToolCallStatus, ToolRiskLevel
from app.models.observability import ToolCallLog


async def create_log(
    db: AsyncSession,
    *,
    ws_id: uuid.UUID,
    session_id: uuid.UUID | None,
    tool_name: str,
    risk_level: ToolRiskLevel,
    args: dict,
    status: ToolCallStatus,
) -> ToolCallLog:
    """新建一条调用审计记录。

    Args:
        db: 数据库会话。
        ws_id: 所属 workspace。
        session_id: 来源会话（REST 直调可空）。
        tool_name: 工具名。
        risk_level: 风险分级。
        args: 校验后的调用参数。
        status: 初始状态（external 为 pending，其余直入终态）。

    Returns:
        落库后的审计记录。
    """
    log = ToolCallLog(
        workspace_id=ws_id,
        session_id=session_id,
        tool_name=tool_name,
        risk_level=risk_level,
        args=args,
        status=status,
    )
    db.add(log)
    await db.flush()
    return log


async def get_log(db: AsyncSession, *, ws_id: uuid.UUID, call_id: uuid.UUID) -> ToolCallLog | None:
    """按 workspace + ID 取审计记录（隔离第一道防线）。

    Args:
        db: 数据库会话。
        ws_id: 所属 workspace。
        call_id: 调用记录 ID。

    Returns:
        命中的记录；无则 None。
    """
    result = await db.execute(
        select(ToolCallLog).where(ToolCallLog.workspace_id == ws_id, ToolCallLog.id == call_id)
    )
    return result.scalar_one_or_none()


async def list_logs(
    db: AsyncSession,
    *,
    ws_id: uuid.UUID,
    session_id: uuid.UUID | None,
    limit: int,
) -> list[ToolCallLog]:
    """审计清单（created_at 倒序，最近调用在前）。

    Args:
        db: 数据库会话。
        ws_id: 所属 workspace。
        session_id: 会话过滤；None 为全部。
        limit: 条数上限。

    Returns:
        审计记录列表。
    """
    stmt = select(ToolCallLog).where(ToolCallLog.workspace_id == ws_id)
    if session_id is not None:
        stmt = stmt.where(ToolCallLog.session_id == session_id)
    stmt = stmt.order_by(ToolCallLog.created_at.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())
