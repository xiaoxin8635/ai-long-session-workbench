"""知识库用例编排（M-07，docs/06 §9）。

上传（同步解析/切片/分词 + 后台向量化）、列表、删除（硬删级联）与
检索调试（与 chat 链路共用 KnowledgeRetriever，口径一致）。
"""

import hashlib
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.context.tokenizer import count_tokens
from app.core.config import get_settings
from app.core.errors import AppError, NotFoundError
from app.llm.embeddings import EmbeddingError, get_embedding_client
from app.llm.rerank import RerankError, get_rerank_client
from app.models.enums import KnowledgeFileStatus
from app.models.knowledge import KnowledgeFile
from app.models.user import User
from app.rag.chunker import split_chunks
from app.rag.ingest import spawn_ingest
from app.rag.parser import parse_document
from app.rag.retriever import KnowledgeRetriever
from app.rag.schemas import CitedChunk
from app.rag.tokenizer import tokenize_for_index
from app.repositories import knowledge_repo

logger = logging.getLogger(__name__)

# 文件名存储上限（与 ORM String(255) 对齐）
_FILENAME_MAX = 255

# 摄取未完成的文件重传时的重试派发口径（PARSING 卡死 / FAILED 可重试）
_RETRYABLE = (KnowledgeFileStatus.PARSING, KnowledgeFileStatus.FAILED)


class KnowledgeService:
    """知识库 API 用例编排（路由层只做参数校验与模型转换）。"""

    async def upload(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        user: User,
        filename: str,
        data: bytes,
    ) -> tuple[KnowledgeFile, int, bool]:
        """上传文件：查重 → 同步解析切片分词 → 落库 → 派发后台向量化。

        同名重传视为新版本：旧文件置 SUPERSEDED（向量与分词双下线），
        新文件 version = 旧 version + 1。checksum 命中时幂等返回已有文件
        （摄取未完成则重派发摄取任务）。

        Args:
            db: 数据库会话（请求级；本方法内部显式提交后派发后台任务）。
            ws_id: 所属 workspace。
            user: 上传用户（记 uploaded_by）。
            filename: 原始文件名（扩展名决定解析器）。
            data: 文件原始字节（不留存，仅存解析产物与摘要）。

        Returns:
            (文件对象, 切片数, 是否新建) 三元组。

        Raises:
            AppError: 413 文件超过大小上限。
            ParseError(AppError): 422 类型不支持/解析失败/内容为空。
        """
        settings = get_settings()
        max_bytes = settings.rag_max_upload_mb * 1024 * 1024
        if len(data) > max_bytes:
            raise AppError(
                "knowledge_file_too_large", 413, f"文件超过 {settings.rag_max_upload_mb}MB 上限"
            )

        # ---- 内容去重（SHA-256；命中直接返回，未完成摄取重派发）----
        checksum = hashlib.sha256(data).hexdigest()
        existing = await knowledge_repo.find_by_checksum(db, ws_id=ws_id, checksum=checksum)
        if existing is not None:
            chunk_count = await knowledge_repo.count_chunks(db, file_id=existing.id)
            if existing.status in _RETRYABLE:
                await db.commit()  # 先落库再派发（后台任务独立会话需读到行）
                spawn_ingest(existing.id)
            return existing, chunk_count, False

        # ---- 同步解析 + 切片 + 分词（CPU 快，请求内完成）----
        file_type, text = parse_document(filename, data)
        pieces = split_chunks(
            text,
            target_tokens=settings.rag_chunk_tokens,
            overlap_ratio=settings.rag_chunk_overlap_ratio,
        )
        chunks = [
            {
                "chunk_index": index,
                "content": piece,
                "token_count": count_tokens(piece),
                "tokens": tokenize_for_index(piece),
            }
            for index, piece in enumerate(pieces)
        ]

        # ---- 同名版本链：旧版下线（向量与分词双清，文本保留可追溯）----
        previous = await knowledge_repo.find_latest_by_filename(db, ws_id=ws_id, filename=filename)
        version = previous.version + 1 if previous is not None else 1
        if previous is not None:
            await knowledge_repo.mark_superseded(db, previous)

        file = await knowledge_repo.create_file_with_chunks(
            db,
            ws_id=ws_id,
            filename=filename[:_FILENAME_MAX],
            file_type=file_type,
            checksum=checksum,
            uploaded_by=user.id,
            version=version,
            chunks=chunks,
        )
        await db.commit()  # 先落库再派发（后台任务独立会话需读到行）
        spawn_ingest(file.id)
        logger.info(
            "knowledge_uploaded file_id=%s file=%s version=%s chunks=%s",
            file.id,
            filename,
            version,
            len(chunks),
        )
        return file, len(chunks), True

    async def list_files(
        self, db: AsyncSession, *, ws_id: uuid.UUID
    ) -> list[tuple[KnowledgeFile, int]]:
        """列出 workspace 全部文件及切片数（新上传在前）。"""
        return await knowledge_repo.list_files(db, ws_id=ws_id)

    async def delete(self, db: AsyncSession, *, ws_id: uuid.UUID, file_id: uuid.UUID) -> None:
        """硬删文件（级联删切片；删除后检索不再命中）。

        Raises:
            NotFoundError: 文件不存在或不属于该 workspace。
        """
        file = await knowledge_repo.get_file(db, ws_id=ws_id, file_id=file_id)
        if file is None:
            raise NotFoundError("文件")
        await knowledge_repo.delete_file(db, file)

    async def search(
        self, db: AsyncSession, *, ws_id: uuid.UUID, query: str, top_k: int | None = None
    ) -> list[CitedChunk]:
        """混合检索（调试口径与 chat 链路 RAG 区块完全一致）。

        Args:
            db: 数据库会话。
            ws_id: workspace 隔离边界。
            query: 检索查询。
            top_k: 返回条数；None 用配置默认。

        Returns:
            综合分降序的命中列表（embedding/rerank 不可用时对应路降级）。
        """
        try:
            embedding = get_embedding_client()
        except EmbeddingError:
            embedding = None  # 退化为纯 BM25
        try:
            rerank = get_rerank_client()
        except RerankError:
            rerank = None  # 降级 RRF 直排
        retriever = KnowledgeRetriever(embedding=embedding, rerank=rerank)
        return await retriever.search(db, ws_id=ws_id, query=query, top_k=top_k)
