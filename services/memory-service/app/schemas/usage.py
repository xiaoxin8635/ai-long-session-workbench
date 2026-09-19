"""用量统计 API 的响应模型（M-11，docs/01 §7 / docs/05 §7.2 看板数据源）。"""

from datetime import date

from pydantic import BaseModel, computed_field


class UsageTotals(BaseModel):
    """窗口内 token 总量与区块拆分（看板"总计/区块占比"数据）。"""

    prompt_tokens: int
    completion_tokens: int
    memory_tokens: int
    rag_tokens: int
    tool_tokens: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_tokens(self) -> int:
        """总 token（prompt + completion）。"""
        return self.prompt_tokens + self.completion_tokens


class UsageDayItem(BaseModel):
    """按日聚合项（看板趋势图数据）。"""

    day: date
    prompt_tokens: int
    completion_tokens: int


class UsageSummaryResponse(BaseModel):
    """GET /api/usage/summary 响应（聚合 token_usages 表）。"""

    workspace_id: str
    days: int
    totals: UsageTotals
    sessions: int  # 窗口内有用量的会话数
    turns: int  # 窗口内对话轮次数
    by_day: list[UsageDayItem]
