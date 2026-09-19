"""用量统计端点（M-11，docs/01 §7 / docs/05 §7.2 看板数据源）。

  GET /api/usage/summary?workspace_id=&days=7
    聚合 token_usages：总量与区块拆分（memory/rag/tool）、会话数、轮次数、按日序列。

鉴权：Bearer JWT + workspace 成员校验（CurrentWorkspaceQuery，403 防枚举）。
"""

from typing import Annotated

from fastapi import APIRouter, Query

from app.core.deps import CurrentWorkspaceQuery, DbDep
from app.repositories import usage_repo
from app.schemas.usage import UsageDayItem, UsageSummaryResponse, UsageTotals

router = APIRouter(prefix="/api/usage", tags=["usage"])

DaysQuery = Annotated[int, Query(ge=1, le=90, description="统计窗口天数（1-90）")]


@router.get("/summary", response_model=UsageSummaryResponse, summary="token 用量聚合")
async def usage_summary(
    ws: CurrentWorkspaceQuery, db: DbDep, days: DaysQuery = 7
) -> UsageSummaryResponse:
    """看板数据源：窗口内 token 总量、区块占比、会话数、轮次数与按日趋势。"""
    result = await usage_repo.summary(db, ws_id=ws.id, days=days)
    totals = result["totals"]
    return UsageSummaryResponse(
        workspace_id=str(ws.id),
        days=days,
        totals=UsageTotals(**totals),  # type: ignore[arg-type]
        sessions=int(result["sessions"]),
        turns=int(result["turns"]),
        by_day=[
            UsageDayItem(day=day, prompt_tokens=prompt, completion_tokens=completion)
            for day, prompt, completion in result["by_day"]
        ],
    )
