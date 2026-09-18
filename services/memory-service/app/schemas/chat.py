"""Chat 端点 schema（OpenAI 兼容协议 + metadata 扩展，docs/01 §6.1）。"""

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """OpenAI 格式消息（M1 直通阶段仅消费 role/content）。"""

    role: str = Field(pattern="^(user|assistant|system|tool)$")
    content: str


class ChatMetadata(BaseModel):
    """EchoDesk 扩展 metadata（Open WebUI Pipe 透传）。

    Attributes:
        workspace_id: 目标 workspace（鉴权与隔离用，必填）。
        session_id: 目标会话；缺省时自动创建新会话。
    """

    workspace_id: str = Field(min_length=1)
    session_id: str | None = None


class ChatCompletionRequest(BaseModel):
    """POST /v1/chat/completions 请求体。"""

    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = True
    metadata: ChatMetadata
    temperature: float | None = Field(default=None, ge=0, le=2)


class ChatUsage(BaseModel):
    """OpenAI usage 结构。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
