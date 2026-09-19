"""全服务共享枚举（存储为字符串，便于人工排查与跨端一致）。"""

from enum import StrEnum


class MemberRole(StrEnum):
    """workspace 成员角色（RBAC，docs/01 §5.8）。"""

    OWNER = "owner"  # 工作区所有者（唯一）
    ADMIN = "admin"  # 可管理成员与知识库
    MEMBER = "member"  # 普通使用


class SessionStatus(StrEnum):
    """会话状态。"""

    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"  # 软删除


class MessageRole(StrEnum):
    """消息角色（对齐 OpenAI chat 协议 + 工具消息）。"""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class MemoryType(StrEnum):
    """长期记忆类型（Working 在 Redis、Knowledge 在向量库，均不落 memories 表）。"""

    EPISODIC = "episodic"  # 会话摘要/事件
    SEMANTIC = "semantic"  # 用户事实与背景
    PROCEDURAL = "procedural"  # 偏好与流程


class MemoryStatus(StrEnum):
    """记忆状态机（docs/01 §5.2.2）。"""

    ACTIVE = "active"
    SUPERSEDED = "superseded"  # 被新版本替代（版本链保留）
    CONFLICTED = "conflicted"  # 冲突待用户裁决
    ARCHIVED = "archived"  # 手动归档
    DELETED = "deleted"  # 软删除


class MemoryEventType(StrEnum):
    """记忆事件流水类型（memory_events 表）。"""

    CREATED = "create"
    UPDATED = "update"
    MERGED = "merge"
    CONFLICT = "conflict"
    SUPERSEDED = "supersede"
    HIT = "hit"  # 被检索注入
    EXPIRED = "expire"
    EDITED_BY_USER = "edit_by_user"
    DELETED = "delete"


class MemoryEventSource(StrEnum):
    """事件来源。"""

    LLM = "llm"  # 抽取管线自动
    USER = "user"  # 用户手动操作
    SYSTEM = "system"


class KnowledgeFileStatus(StrEnum):
    """知识库文件摄取状态机。

    SUPERSEDED：同名重传后被新版本替代（旧版向量已下线、文本保留可追溯）。
    """

    PARSING = "parsing"
    EMBEDDED = "embedded"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class TaskStatus(StrEnum):
    """任务/待办状态。"""

    OPEN = "open"
    DOING = "doing"
    DONE = "done"
    ABANDONED = "abandoned"


class ToolRiskLevel(StrEnum):
    """工具风险分级（docs/01 §5.6）。"""

    READ_ONLY = "read_only"  # 自动执行
    WRITE = "write"  # 自动执行 + 审计
    EXTERNAL = "external"  # 需用户确认


class ToolCallStatus(StrEnum):
    """工具调用结果状态。"""

    SUCCESS = "success"
    DENIED = "denied"  # 用户拒绝确认
    TIMEOUT = "timeout"  # 确认等待超时
    FAILED = "failed"  # 执行异常
