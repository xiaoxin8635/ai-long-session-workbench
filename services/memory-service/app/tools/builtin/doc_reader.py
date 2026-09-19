"""文档读取工具（M-09，read_only 风险）：doc.read。

读取知识库中指定文件的最新版本全文（按 chunk 正序拼接，超长截断）。
与 RAG 检索互补：检索按相关性给片段，本工具按文件名给全量上下文。
"""

import uuid

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.enums import ToolRiskLevel
from app.models.user import User
from app.repositories import knowledge_repo
from app.tools.registry import ToolDefinition, ToolRegistry, default_registry


class DocReadArgs(BaseModel):
    """doc.read 参数。"""

    filename: str = Field(min_length=1, max_length=255, description="知识库文件名（含扩展名）")
    max_chars: int = Field(default=4000, ge=200, le=20000, description="返回正文上限")


async def _read(
    db: AsyncSession,
    *,
    ws_id: uuid.UUID,
    user: User,
    session_id: uuid.UUID | None,
    args: DocReadArgs,
) -> dict:
    """读文件最新版本全文（找不到或为空时显式返回 found=False）。"""
    file = await knowledge_repo.find_latest_by_filename(db, ws_id=ws_id, filename=args.filename)
    if file is None:
        return {"found": False, "filename": args.filename}
    chunks = await knowledge_repo.list_chunks(db, file_id=file.id)
    text = "\n\n".join(chunk.content for chunk in chunks)
    limit = min(args.max_chars, get_settings().tool_result_max_chars * 4)
    truncated = len(text) > limit
    return {
        "found": True,
        "filename": file.filename,
        "version": file.version,
        "chunk_count": len(chunks),
        "truncated": truncated,
        "text": text[:limit],
    }


def register(registry: ToolRegistry) -> None:
    """向注册表登记文档读取工具（builtin 包导入时调用）。

    Args:
        registry: 目标注册表。
    """
    registry.register(
        ToolDefinition(
            name="doc.read",
            description="按文件名读取用户知识库文档的最新版本全文（chunk 正序拼接）",
            risk=ToolRiskLevel.READ_ONLY,
            args_model=DocReadArgs,
            handler=_read,
        )
    )


register(default_registry)
