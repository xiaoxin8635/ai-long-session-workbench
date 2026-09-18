"""Context preview 请求/响应模型（M-05 调试端点 /api/context/preview）。"""

from pydantic import BaseModel, Field


class ContextPreviewRequest(BaseModel):
    """上下文装配预览请求体。

    Attributes:
        workspace_id: 所属 workspace UUID（成员校验用）。
        session_id: 目标会话 UUID（窗口与滚动摘要来源）。
        query: 模拟的当前用户问题（长期记忆检索的查询）。
        profile: 预算 profile 名；缺省用服务配置 context_budget_profile。
    """

    workspace_id: str = Field(..., min_length=1, description="workspace UUID")
    session_id: str = Field(..., min_length=1, description="会话 UUID")
    query: str = Field(..., min_length=1, description="模拟当前问题")
    profile: str | None = Field(None, description="预算 profile，缺省 default")


class ContextSectionUsage(BaseModel):
    """单区块装配用量明细。

    Attributes:
        key: 区块标识（system/procedural/semantic/episodic/working/rag/tool_results）。
        budget_tokens: 区块预算（system 为 -1 表示不裁剪）。
        included: 实际纳入条数。
        dropped: 裁剪丢弃条数。
        tokens: 实际 token 占用。
    """

    key: str
    budget_tokens: int
    included: int
    dropped: int
    tokens: int


class ContextPreviewResponse(BaseModel):
    """上下文装配预览结果（messages 与区块用量，不调用 LLM）。

    Attributes:
        profile: 实际使用的预算 profile 名。
        window_tokens: 总窗口（context_window_tokens）。
        available_tokens: 区块可用总量（窗口减输出预留）。
        total_tokens: 本次装配 token 总和。
        sections: 各区块用量（装配顺序）。
        messages: 装配产物（可直接送 LLM 的 OpenAI messages）。
    """

    profile: str
    window_tokens: int
    available_tokens: int
    total_tokens: int
    sections: list[ContextSectionUsage]
    messages: list[dict[str, str]]
