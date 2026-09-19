"""工具执行器（M-09，docs/01 §5.6 / docs/06 §11）：分级权限 + 确认流 + 审计。

  read_only → 直接执行（审计）
  write     → 直接执行（审计）
  external  → 落 pending 审计 → 202 返回 call_id → 后台任务经 Redis 等待
              用户确认（approve/deny），超时（默认 60s）置 timeout 拒绝

降级契约（DoD）：工具异常不炸调用方——立即路径返回 status=failed + 错误
说明；确认流路径由后台任务落 FAILED 终态。结果一律摘要化（截断上限由
Settings 控制）后写 result_digest，不存全量。
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_redis
from app.core.errors import AppError
from app.db.session import get_session_factory
from app.models.enums import ToolCallStatus, ToolRiskLevel
from app.models.user import User
from app.observability import tracing
from app.repositories import tool_call_repo
from app.tools.registry import ToolDefinition, default_registry

logger = logging.getLogger(__name__)

# 后台确认等待任务的引用池（防 GC 回收；与 M-07 ingest 同款模式）
_BACKGROUND: set[asyncio.Task[None]] = set()

# Redis 确认键与取值（TTL 由等待窗口 + 30s 余量保证覆盖）
_CONFIRM_KEY_PREFIX = "tool:confirm:"
_CONFIRM_APPROVE = "approve"
_CONFIRM_DENY = "deny"
_POLL_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class ToolExecutionResult:
    """一次工具调用的执行结果（REST 响应与内部调用共用）。"""

    call_id: uuid.UUID
    status: ToolCallStatus
    requires_confirmation: bool
    result: dict[str, Any] | None = None
    error: str | None = None


def digest_result(result: dict[str, Any]) -> str:
    """结果摘要化：JSON 序列化后超限截断并标注（审计不存全量）。

    Args:
        result: 工具返回的结构化结果。

    Returns:
        可入库的 result_digest 文本。
    """
    text = json.dumps(result, ensure_ascii=False, default=str)
    limit = get_settings().tool_result_max_chars
    if len(text) <= limit:
        return text
    return text[:limit] + f"…[已截断，原始 {len(text)} 字符]"


def validate_args(tool: ToolDefinition, raw_args: dict[str, Any]) -> Any:
    """按工具参数模型校验调用参数。

    Args:
        tool: 工具定义。
        raw_args: 调用方提交的原始参数。

    Returns:
        校验后的参数实例（args_model 为 None 时返回 None）。

    Raises:
        AppError: 422 —— 参数不满足 schema。
    """
    if tool.args_model is None:
        if raw_args:
            raise AppError("tool_args_invalid", 422, f"工具 {tool.name} 不接受参数")
        return None
    try:
        return tool.args_model(**raw_args)
    except ValidationError as exc:
        raise AppError("tool_args_invalid", 422, exc.errors(include_url=False)) from exc


async def execute_tool(
    db: AsyncSession,
    *,
    ws_id: uuid.UUID,
    user: User,
    name: str,
    raw_args: dict[str, Any],
    session_id: uuid.UUID | None = None,
) -> ToolExecutionResult:
    """按风险分级执行工具（M-09 主入口）。

    Args:
        db: 请求级数据库会话。
        ws_id: 所属 workspace。
        user: 调用者（external 确认人预备）。
        name: 工具名。
        raw_args: 原始参数。
        session_id: 来源会话（审计挂链，可空）。

    Returns:
        执行结果（external 为 requires_confirmation=True 的 pending 态）。

    Raises:
        AppError: 404 工具未注册 / 422 参数不合法。
    """
    tool = default_registry.get(name)
    if tool is None:
        raise AppError("tool_not_found", 404, f"工具 {name} 未注册")
    args = validate_args(tool, raw_args)

    if tool.risk is ToolRiskLevel.EXTERNAL:
        log = await tool_call_repo.create_log(
            db,
            ws_id=ws_id,
            session_id=session_id,
            tool_name=tool.name,
            risk_level=tool.risk,
            args=raw_args,
            status=ToolCallStatus.PENDING,
        )
        # 必须先提交：后台等待任务用独立会话读该行（M-07 同款竞态教训）
        await db.commit()
        task = asyncio.create_task(
            _wait_confirm_and_execute(
                call_id=log.id,
                tool=tool,
                raw_args=raw_args,
                ws_id=ws_id,
                user_id=user.id,
                session_id=session_id,
            )
        )
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
        return ToolExecutionResult(
            call_id=log.id, status=ToolCallStatus.PENDING, requires_confirmation=True
        )

    log = await tool_call_repo.create_log(
        db,
        ws_id=ws_id,
        session_id=session_id,
        tool_name=tool.name,
        risk_level=tool.risk,
        args=raw_args,
        status=ToolCallStatus.SUCCESS,  # 先占位，执行后按实际改写
    )
    try:
        # tool.invoke span（M-12 埋点矩阵）：覆盖 handler 执行全程，终态进
        # metadata（未配置观测时 no-op）
        with tracing.span(
            "tool.invoke",
            tool=tool.name,
            risk=tool.risk.value,
            workspace_id=str(ws_id),
            session_id=str(session_id) if session_id else None,
            mode="immediate",
        ) as obs:
            result = await tool.handler(
                db, ws_id=ws_id, user=user, session_id=session_id, args=args
            )
            obs.update(metadata={"status": ToolCallStatus.SUCCESS.value})
    except Exception as exc:  # 工具异常降级为文本说明，不炸调用方（DoD）
        logger.warning("tool_execute_failed tool=%s error=%s", tool.name, exc)
        log.status = ToolCallStatus.FAILED
        log.result_digest = f"error: {exc}"[: get_settings().tool_result_max_chars]
        await db.commit()
        return ToolExecutionResult(
            call_id=log.id,
            status=ToolCallStatus.FAILED,
            requires_confirmation=False,
            error=str(exc),
        )
    log.status = ToolCallStatus.SUCCESS
    log.result_digest = digest_result(result)
    await db.commit()
    return ToolExecutionResult(
        call_id=log.id,
        status=ToolCallStatus.SUCCESS,
        requires_confirmation=False,
        result=result,
    )


async def confirm_tool_call(
    db: AsyncSession,
    *,
    ws_id: uuid.UUID,
    user: User,
    call_id: uuid.UUID,
    approve: bool,
) -> ToolCallStatus:
    """确认/拒绝一条 pending 的 external 调用（经 Redis 通知等待任务）。

    Args:
        db: 请求级数据库会话。
        ws_id: 所属 workspace。
        user: 确认人（落 approver_id 审计）。
        call_id: 调用记录 ID。
        approve: True 执行 / False 拒绝。

    Returns:
        确认后的瞬时状态（pending 任务的终态由后台落库）。

    Raises:
        AppError: 404 记录不存在 / 409 状态已非 pending。
    """
    log = await tool_call_repo.get_log(db, ws_id=ws_id, call_id=call_id)
    if log is None:
        raise AppError("tool_call_not_found", 404, "调用记录不存在")
    if log.status != ToolCallStatus.PENDING:  # StrEnum：DB 回读为 str，必须 ==
        raise AppError("tool_call_not_pending", 409, f"该调用已处于 {log.status} 终态")
    log.approver_id = user.id
    await db.commit()
    settings = get_settings()
    redis = get_redis()
    await redis.set(
        _CONFIRM_KEY_PREFIX + str(call_id),
        _CONFIRM_APPROVE if approve else _CONFIRM_DENY,
        ex=int(settings.tool_confirm_timeout_seconds) + 30,
    )
    return ToolCallStatus.PENDING


async def _wait_confirm_and_execute(
    *,
    call_id: uuid.UUID,
    tool: ToolDefinition,
    raw_args: dict[str, Any],
    ws_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID | None,
) -> None:
    """external 工具的确认等待与执行（后台任务，独立数据库会话）。

    轮询 Redis 确认键：approve → 执行（成功/失败）；deny → 拒绝；
    超时 → timeout 拒绝。全部路径落终态审计。

    Args:
        call_id: 调用记录 ID。
        tool: 工具定义。
        raw_args: 已校验的原始参数。
        ws_id: 所属 workspace。
        user_id: 发起用户（重建轻量调用上下文）。
        session_id: 来源会话。
    """
    settings = get_settings()
    deadline = time.monotonic() + settings.tool_confirm_timeout_seconds
    decision: str | None = None
    redis = get_redis()
    key = _CONFIRM_KEY_PREFIX + str(call_id)
    while time.monotonic() < deadline:
        value = await redis.get(key)
        if value in (_CONFIRM_APPROVE, _CONFIRM_DENY):
            decision = value
            break
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    await redis.delete(key)

    async with get_session_factory()() as db:
        log = await tool_call_repo.get_log(db, ws_id=ws_id, call_id=call_id)
        if log is None or log.status != ToolCallStatus.PENDING:  # 已被并发改写则不重复处理
            return
        if decision is None:
            log.status = ToolCallStatus.TIMEOUT
            log.result_digest = "等待用户确认超时，已拒绝执行"
        elif decision == _CONFIRM_DENY:
            log.status = ToolCallStatus.DENIED
            log.result_digest = "用户拒绝执行"
        else:
            try:
                args = validate_args(tool, raw_args)
                # tool.invoke span（external 确认后执行；独立根——后台任务
                # 无请求上下文，与触发的 chat.turn 之间以 call_id 关联）
                with tracing.span(
                    "tool.invoke",
                    tool=tool.name,
                    risk=tool.risk.value,
                    workspace_id=str(ws_id),
                    session_id=str(session_id) if session_id else None,
                    mode="confirmed",
                ) as obs:
                    result = await tool.handler(
                        db,
                        ws_id=ws_id,
                        user=_minimal_user(user_id),
                        session_id=session_id,
                        args=args,
                    )
                    obs.update(metadata={"status": ToolCallStatus.SUCCESS.value})
                log.status = ToolCallStatus.SUCCESS
                log.result_digest = digest_result(result)
            except Exception as exc:
                logger.warning("tool_confirm_execute_failed tool=%s error=%s", tool.name, exc)
                log.status = ToolCallStatus.FAILED
                log.result_digest = f"error: {exc}"[: settings.tool_result_max_chars]
        await db.commit()


def _minimal_user(user_id: uuid.UUID) -> User:
    """按 ID 构造最小用户上下文（后台任务无请求态，handler 仅可用身份字段）。

    Args:
        user_id: 发起用户 ID。

    Returns:
        仅携带 id/is_active 的瞬态 User 对象（不落库）。
    """
    return User(id=user_id, username="", password_hash="", is_active=True)
