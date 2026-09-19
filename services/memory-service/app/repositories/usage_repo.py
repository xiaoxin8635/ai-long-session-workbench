"""token 用量仓储（M-11，docs/01 §4.1 token_usages 表的读写收口）。

写入：chat 每轮 finalize 时落一条（区块明细取 M-05 装配 sections；
prompt/completion 优先上游 usage，流式缺失时本地启发式估算）。
聚合：GET /api/usage/summary 的 SQL 侧汇总（总量/区块/会话数/轮次/按日）。
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context.schemas import AssembledContext, SectionKey
from app.context.tokenizer import count_tokens
from app.models.observability import TokenUsage

# 记忆类区块（token_usages.memory_tokens 的聚合口径）
_MEMORY_SECTIONS = (SectionKey.PROCEDURAL, SectionKey.SEMANTIC, SectionKey.EPISODIC)


async def next_turn_no(db: AsyncSession, *, session_id: uuid.UUID) -> int:
    """取会话内下一轮次号（已有最大 turn_no + 1，首轮为 1）。

    Args:
        db: 数据库会话。
        session_id: 目标会话。

    Returns:
        轮次号（无唯一约束；同会话极端并发下可能重号，看板统计可容忍）。
    """
    result = await db.execute(
        select(func.coalesce(func.max(TokenUsage.turn_no), 0)).where(
            TokenUsage.session_id == session_id
        )
    )
    return int(result.scalar_one()) + 1


async def record_turn(
    db: AsyncSession,
    *,
    ws_id: uuid.UUID,
    session_id: uuid.UUID,
    answer: str,
    assembled: AssembledContext | None = None,
    usage: object | None = None,
) -> TokenUsage:
    """落一条轮次用量（chat finalize 调用，与消息同一事务提交）。

    Args:
        db: 数据库会话。
        ws_id: 所属 workspace。
        session_id: 会话。
        answer: 完整回答文本（流式本地估算 completion 用）。
        assembled: 本轮上下文装配结果（区块明细来源）；None 时区块全 0。
        usage: 上游 LLM usage（含 prompt_tokens/completion_tokens 属性）；
            流式或未回传时 None → 全量本地启发式估算。

    Returns:
        落库后的 TokenUsage 对象（未提交，由调用方统一 commit）。
    """
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    if assembled is not None:
        # 上游未回传 usage（流式常见）→ 本地启发式估算
        if prompt_tokens == 0:
            prompt_tokens = sum(count_tokens(m["content"]) for m in assembled.messages)
        if completion_tokens == 0:
            completion_tokens = count_tokens(answer)
        section_tokens = {s.key: s.tokens for s in assembled.sections}
        memory_tokens = sum(section_tokens.get(key, 0) for key in _MEMORY_SECTIONS)
        rag_tokens = section_tokens.get(SectionKey.RAG, 0)
        tool_tokens = section_tokens.get(SectionKey.TOOL_RESULTS, 0)
    else:
        memory_tokens = rag_tokens = tool_tokens = 0
    turn_no = await next_turn_no(db, session_id=session_id)
    entry = TokenUsage(
        workspace_id=ws_id,
        session_id=session_id,
        turn_no=turn_no,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        memory_tokens=memory_tokens,
        rag_tokens=rag_tokens,
        tool_tokens=tool_tokens,
        # 成本折算需模型价目表（M-12 观测配置统一引入），当前恒 0
        cost_usd=0,
    )
    db.add(entry)
    await db.flush()
    return entry


async def summary(db: AsyncSession, *, ws_id: uuid.UUID, days: int) -> dict[str, object]:
    """聚合窗口内用量（SQL 侧汇总，看板数据源）。

    Args:
        db: 数据库会话。
        ws_id: 所属 workspace。
        days: 统计窗口天数（created_at >= now - days）。

    Returns:
        {totals: {...五项 token 和}, sessions, turns,
         by_day: [(day, prompt, completion)]——day 为 UTC 日期，日界口径统一}；
        无数据时 totals 全零、by_day 空列表。
    """
    since = datetime.now(UTC) - timedelta(days=days)
    totals_row = (
        await db.execute(
            select(
                func.coalesce(func.sum(TokenUsage.prompt_tokens), 0).label("prompt"),
                func.coalesce(func.sum(TokenUsage.completion_tokens), 0).label("completion"),
                func.coalesce(func.sum(TokenUsage.memory_tokens), 0).label("memory"),
                func.coalesce(func.sum(TokenUsage.rag_tokens), 0).label("rag"),
                func.coalesce(func.sum(TokenUsage.tool_tokens), 0).label("tool"),
                func.count(TokenUsage.id).label("turns"),
                func.count(func.distinct(TokenUsage.session_id)).label("sessions"),
            ).where(TokenUsage.workspace_id == ws_id, TokenUsage.created_at >= since)
        )
    ).one()
    # 统一按 UTC 日界聚合：PG 会话 TimeZone（本部署为 Asia/Shanghai）会改变
    # date_trunc 对 timestamptz 的截断基准，而 asyncpg 将 timestamptz 按 UTC
    # 解码、Python 侧再取 .date()——两侧时区不一致时按日序列会整体偏移一天。
    # 先 AT TIME ZONE 'UTC' 转成 naive UTC 时间戳再截断，保证与
    # datetime.now(UTC).date() 口径一致；面向用户的本地化展示由前端换算。
    utc_created = TokenUsage.created_at.op("AT TIME ZONE")("UTC")
    day_expr = func.date_trunc("day", utc_created).label("day")
    by_day_rows = (
        await db.execute(
            select(
                day_expr,
                func.coalesce(func.sum(TokenUsage.prompt_tokens), 0),
                func.coalesce(func.sum(TokenUsage.completion_tokens), 0),
            )
            .where(TokenUsage.workspace_id == ws_id, TokenUsage.created_at >= since)
            .group_by(day_expr)
            .order_by(day_expr)
        )
    ).all()
    return {
        "totals": {
            "prompt_tokens": int(totals_row.prompt),
            "completion_tokens": int(totals_row.completion),
            "memory_tokens": int(totals_row.memory),
            "rag_tokens": int(totals_row.rag),
            "tool_tokens": int(totals_row.tool),
        },
        "sessions": int(totals_row.sessions),
        "turns": int(totals_row.turns),
        "by_day": [(row[0].date(), int(row[1]), int(row[2])) for row in by_day_rows],
    }
