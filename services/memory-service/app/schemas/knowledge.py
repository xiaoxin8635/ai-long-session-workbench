"""知识库 API 模型（M-07，docs/06 §9）。

上传/列表响应、检索调试请求与命中项、chat 响应 metadata.citations 引用项。
"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import KnowledgeFileStatus
from app.rag.schemas import CitedChunk


class KnowledgeFileRead(BaseModel):
    """知识文件元信息（上传 201 响应 / 列表项）。"""

    id: str
    filename: str
    file_type: str
    status: KnowledgeFileStatus
    version: int
    chunk_count: int
    created_at: datetime


class KnowledgeSearchRequest(BaseModel):
    """POST /api/knowledge/search 检索调试请求体。"""

    query: str = Field(
        min_length=1, max_length=2000, description="检索查询（调试口径与 chat 链路一致）"
    )
    top_k: int | None = Field(default=None, ge=1, le=20, description="返回条数；缺省用服务配置")


class KnowledgeHit(BaseModel):
    """检索调试命中项（含正文与双路分数，供召回率观测）。"""

    chunk_id: str
    file_id: str
    filename: str
    chunk_index: int
    content: str
    score: float
    vector_similarity: float


class KnowledgeSearchResponse(BaseModel):
    """检索调试响应（回显 query 便于对照）。"""

    query: str
    hits: list[KnowledgeHit]


class Citation(BaseModel):
    """chat 响应 metadata.citations 引用项（DoD：定位到 chunk）。"""

    chunk_id: str
    file_id: str
    filename: str
    chunk_index: int
    score: float

    @classmethod
    def from_cited(cls, cited: CitedChunk) -> "Citation":
        """从检索器命中构造（chat 路由 metadata.citations 转换）。"""
        return cls(
            chunk_id=str(cited.chunk_id),
            file_id=str(cited.file_id),
            filename=cited.filename,
            chunk_index=cited.chunk_index,
            score=cited.score,
        )
