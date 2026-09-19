"""知识库仓储（M-07，docs/06 §9）。

向量检索免 join 设计：supersede 时把旧版 chunks 的 embedding 置 NULL、
tokens 置空（BM25 路同步下线），两条检索路均以 chunk 行自身状态过滤，
无需回查 knowledge_files.status。删除为硬删（CASCADE chunks）。
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import KnowledgeFileStatus
from app.models.knowledge import KnowledgeChunk, KnowledgeFile


async def create_file_with_chunks(
    db: AsyncSession,
    *,
    ws_id: uuid.UUID,
    filename: str,
    file_type: str,
    checksum: str,
    uploaded_by: uuid.UUID,
    version: int,
    chunks: list[dict],
) -> KnowledgeFile:
    """创建文件与其全部切片（同事务；embedding 留空待后台管线回填）。

    Args:
        db: 数据库会话。
        ws_id: 所属 workspace。
        filename: 文件名（含扩展名）。
        file_type: 类型标识（pdf/docx/md/txt）。
        checksum: 原始字节 SHA-256。
        uploaded_by: 上传用户。
        version: 版本号（重传递增）。
        chunks: 切片数据列表，元素含 chunk_index/content/token_count/tokens。

    Returns:
        已落库的文件对象（status=PARSING）。
    """
    file = KnowledgeFile(
        workspace_id=ws_id,
        filename=filename,
        file_type=file_type,
        checksum=checksum,
        status=KnowledgeFileStatus.PARSING,
        version=version,
        uploaded_by=uploaded_by,
    )
    db.add(file)
    await db.flush()
    for chunk in chunks:
        db.add(
            KnowledgeChunk(
                file_id=file.id,
                workspace_id=ws_id,
                chunk_index=chunk["chunk_index"],
                content=chunk["content"],
                token_count=chunk["token_count"],
                tokens=chunk["tokens"],
            )
        )
    await db.flush()
    return file


async def get_file(
    db: AsyncSession, *, ws_id: uuid.UUID, file_id: uuid.UUID
) -> KnowledgeFile | None:
    """按 workspace 隔离取文件（路由层权限校验用）。

    Args:
        db: 数据库会话。
        ws_id: 所属 workspace（隔离边界）。
        file_id: 文件 ID。

    Returns:
        文件对象；不存在或跨 workspace 返回 None。
    """
    result = await db.execute(
        select(KnowledgeFile).where(
            KnowledgeFile.id == file_id, KnowledgeFile.workspace_id == ws_id
        )
    )
    return result.scalar_one_or_none()


async def get_file_by_id(db: AsyncSession, file_id: uuid.UUID) -> KnowledgeFile | None:
    """仅按 ID 取文件（后台摄取管线内部调用，无 workspace 上下文）。"""
    result = await db.execute(select(KnowledgeFile).where(KnowledgeFile.id == file_id))
    return result.scalar_one_or_none()


async def list_files(db: AsyncSession, *, ws_id: uuid.UUID) -> list[tuple[KnowledgeFile, int]]:
    """列出 workspace 全部文件及切片数（含各状态，新上传在前）。

    Args:
        db: 数据库会话。
        ws_id: 所属 workspace。

    Returns:
        (文件, 切片数) 元组列表，created_at 降序。
    """
    result = await db.execute(
        select(KnowledgeFile, func.count(KnowledgeChunk.id))
        .outerjoin(KnowledgeChunk, KnowledgeChunk.file_id == KnowledgeFile.id)
        .where(KnowledgeFile.workspace_id == ws_id)
        .group_by(KnowledgeFile.id)
        .order_by(KnowledgeFile.created_at.desc())
    )
    return [(row[0], int(row[1])) for row in result.all()]


async def count_chunks(db: AsyncSession, *, file_id: uuid.UUID) -> int:
    """统计文件切片数（上传幂等命中时响应 chunk_count 用）。"""
    result = await db.execute(
        select(func.count(KnowledgeChunk.id)).where(KnowledgeChunk.file_id == file_id)
    )
    return int(result.scalar_one())


async def find_by_checksum(
    db: AsyncSession, *, ws_id: uuid.UUID, checksum: str
) -> KnowledgeFile | None:
    """按内容摘要查重（同 workspace 内重复上传幂等返回已有文件）。"""
    result = await db.execute(
        select(KnowledgeFile).where(
            KnowledgeFile.workspace_id == ws_id, KnowledgeFile.checksum == checksum
        )
    )
    return result.scalar_one_or_none()


async def find_latest_by_filename(
    db: AsyncSession, *, ws_id: uuid.UUID, filename: str
) -> KnowledgeFile | None:
    """取同名最新版本（非 superseded；重传时递增其 version）。

    Returns:
        同名且状态非 SUPERSEDED 的文件；无则 None。
    """
    result = await db.execute(
        select(KnowledgeFile)
        .where(
            KnowledgeFile.workspace_id == ws_id,
            KnowledgeFile.filename == filename,
            KnowledgeFile.status != KnowledgeFileStatus.SUPERSEDED,
        )
        .order_by(KnowledgeFile.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def mark_superseded(db: AsyncSession, file: KnowledgeFile) -> None:
    """旧版本下线：status=SUPERSEDED + 切片检索键双清（向量 + 分词）。

    文本保留可追溯，但两路检索（pgvector / BM25）均不再命中。

    Args:
        db: 数据库会话。
        file: 被新版本替代的旧文件（原地更新 status）。
    """
    file.status = KnowledgeFileStatus.SUPERSEDED
    await db.execute(
        update(KnowledgeChunk)
        .where(KnowledgeChunk.file_id == file.id)
        .values(embedding=None, tokens=[])
    )


async def delete_file(db: AsyncSession, file: KnowledgeFile) -> None:
    """硬删文件（级联删 chunks；DoD：删除后检索不再命中）。"""
    await db.delete(file)


async def vector_search(
    db: AsyncSession, *, ws_id: uuid.UUID, embedding: list[float], limit: int
) -> list[tuple[KnowledgeChunk, float]]:
    """向量召回：workspace 内余弦近邻 top-k（embedding 非空自动排除下线版本）。

    Args:
        db: 数据库会话。
        ws_id: workspace 隔离（chunk 冗余列，免 join file 表）。
        embedding: 查询向量。
        limit: 召回条数。

    Returns:
        (切片, 余弦相似度) 列表，相似度降序。
    """
    similarity = 1 - KnowledgeChunk.embedding.cosine_distance(embedding)
    result = await db.execute(
        select(KnowledgeChunk, similarity.label("sim"))
        .where(
            KnowledgeChunk.workspace_id == ws_id,
            KnowledgeChunk.embedding.is_not(None),
        )
        .order_by(KnowledgeChunk.embedding.cosine_distance(embedding))
        .limit(limit)
    )
    return [(row[0], float(row[1])) for row in result.all()]


async def load_corpus_tokens(
    db: AsyncSession, *, ws_id: uuid.UUID
) -> list[tuple[uuid.UUID, list[str]]]:
    """加载 BM25 语料（全量在库切片的预分词；空 tokens 视为已下线）。

    Args:
        db: 数据库会话。
        ws_id: workspace 隔离。

    Returns:
        (chunk_id, tokens) 列表。
    """
    result = await db.execute(
        select(KnowledgeChunk.id, KnowledgeChunk.tokens).where(
            KnowledgeChunk.workspace_id == ws_id,
            func.jsonb_array_length(KnowledgeChunk.tokens) > 0,
        )
    )
    return [(row[0], list(row[1])) for row in result.all()]


async def fetch_by_ids(
    db: AsyncSession, ids: Sequence[uuid.UUID]
) -> list[tuple[KnowledgeChunk, str]]:
    """按 ID 批量取切片及其文件名（RRF 融合后候选内容回填 + 引用溯源）。

    Args:
        db: 数据库会话。
        ids: 切片 ID 列表。

    Returns:
        (切片, 所属文件名) 列表。
    """
    if not ids:
        return []
    result = await db.execute(
        select(KnowledgeChunk, KnowledgeFile.filename)
        .join(KnowledgeFile, KnowledgeChunk.file_id == KnowledgeFile.id)
        .where(KnowledgeChunk.id.in_(ids))
    )
    return [(row[0], row[1]) for row in result.all()]


async def load_pending_chunks(db: AsyncSession, *, file_id: uuid.UUID) -> list[KnowledgeChunk]:
    """取文件内待向量化切片（embedding 为空，chunk_index 正序；摄取管线用）。"""
    result = await db.execute(
        select(KnowledgeChunk)
        .where(KnowledgeChunk.file_id == file_id, KnowledgeChunk.embedding.is_(None))
        .order_by(KnowledgeChunk.chunk_index)
    )
    return list(result.scalars().all())


async def list_chunks(db: AsyncSession, *, file_id: uuid.UUID) -> list[KnowledgeChunk]:
    """取文件全部切片（chunk_index 正序；doc.read 工具与调试用）。

    Args:
        db: 数据库会话。
        file_id: 文件 ID。

    Returns:
        切片列表（含已下线版本的历史文本，调用方按需过滤）。
    """
    result = await db.execute(
        select(KnowledgeChunk)
        .where(KnowledgeChunk.file_id == file_id)
        .order_by(KnowledgeChunk.chunk_index)
    )
    return list(result.scalars().all())
