"""知识摄入后台管线（M-07：切片向量化 + 状态机推进）。

上传请求内同步完成解析/切片/分词并落库（status=PARSING），随后
fire-and-forget 派发本管线：批量 embedding → 回填向量 → EMBEDDED。
失败置 FAILED（可重新上传触发重试）；文件已删除或被新版本替代则放弃。
模式与 M-04 抽取管线一致：自管理会话 + 异常全吞（记日志不外抛）。
"""

import asyncio
import logging
import uuid

from app.db.session import get_session_factory
from app.llm.embeddings import get_embedding_client
from app.models.enums import KnowledgeFileStatus
from app.repositories import knowledge_repo

logger = logging.getLogger(__name__)

# 后台任务引用池：防止 asyncio.create_task 的任务被 GC 中途取消
_background_tasks: set[asyncio.Task[object]] = set()


def spawn_ingest(file_id: uuid.UUID) -> None:
    """派发后台摄取任务（fire-and-forget，管线内部吞异常）。"""
    task = asyncio.create_task(ingest_embeddings(file_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def ingest_embeddings(file_id: uuid.UUID) -> None:
    """补齐文件全部切片向量并推进状态机（PARSING → EMBEDDED/FAILED）。

    Args:
        file_id: 待摄取文件 ID。
    """
    try:
        client = get_embedding_client()
        async with get_session_factory()() as db:
            file = await knowledge_repo.get_file_by_id(db, file_id)
            if file is None or file.status != KnowledgeFileStatus.PARSING:
                return  # 已删除或已被同名重传下线，静默放弃
            chunks = await knowledge_repo.load_pending_chunks(db, file_id=file_id)
            if chunks:
                vectors = await client.embed([chunk.content for chunk in chunks])
                for chunk, vector in zip(chunks, vectors, strict=True):
                    chunk.embedding = vector
            file.status = KnowledgeFileStatus.EMBEDDED
            await db.commit()
        logger.info("knowledge_ingested file_id=%s chunks=%s", file_id, len(chunks))
    except Exception as exc:
        logger.warning("knowledge_ingest_failed file_id=%s error=%s", file_id, exc)
        await _mark_failed(file_id)


async def _mark_failed(file_id: uuid.UUID) -> None:
    """独立会话把文件置 FAILED（原会话可能已随异常失效；仅 PARSING 时生效）。"""
    try:
        async with get_session_factory()() as db:
            file = await knowledge_repo.get_file_by_id(db, file_id)
            if file is not None and file.status == KnowledgeFileStatus.PARSING:
                file.status = KnowledgeFileStatus.FAILED
                await db.commit()
    except Exception:
        logger.exception("knowledge_mark_failed_error file_id=%s", file_id)
