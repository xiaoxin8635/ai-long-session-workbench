"""知识库端点（M-07，docs/01 §6.4 / docs/06 §9）。

  POST   /api/knowledge/files          上传（multipart，同步解析切片，后台向量化）
  GET    /api/knowledge/files          列表（状态机 + 版本 + 切片数）
  DELETE /api/knowledge/files/{id}     硬删（级联切片，之后检索不再命中）
  POST   /api/knowledge/search         检索调试（与 chat RAG 区块同口径）

鉴权：Bearer JWT + workspace 成员校验（CurrentWorkspaceQuery，403 防枚举）。
"""

import uuid as uuid_mod
from typing import Annotated

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import Response

from app.core.deps import CurrentUser, CurrentWorkspaceQuery, DbDep
from app.schemas.knowledge import (
    KnowledgeFileRead,
    KnowledgeHit,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
)
from app.services.knowledge_service import KnowledgeService

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

_service = KnowledgeService()


@router.post(
    "/files",
    status_code=201,
    response_model=KnowledgeFileRead,
    summary="上传知识文件（pdf/docx/md/txt）",
)
async def upload_knowledge_file(
    ws: CurrentWorkspaceQuery,
    user: CurrentUser,
    db: DbDep,
    file: Annotated[UploadFile, File(description="待上传文件（pdf/docx/md/txt，≤20MB）")],
) -> KnowledgeFileRead:
    """同步完成解析/切片/分词入库（status=parsing），embedding 后台补齐。"""
    data = await file.read()
    filename = file.filename or "untitled.txt"
    kb_file, chunk_count, _created = await _service.upload(
        db, ws_id=ws.id, user=user, filename=filename, data=data
    )
    return KnowledgeFileRead(
        id=str(kb_file.id),
        filename=kb_file.filename,
        file_type=kb_file.file_type,
        status=kb_file.status,
        version=kb_file.version,
        chunk_count=chunk_count,
        created_at=kb_file.created_at,
    )


@router.get("/files", response_model=list[KnowledgeFileRead], summary="知识文件列表")
async def list_knowledge_files(ws: CurrentWorkspaceQuery, db: DbDep) -> list[KnowledgeFileRead]:
    """workspace 内全部文件（含 superseded 历史版本，新上传在前）。"""
    rows = await _service.list_files(db, ws_id=ws.id)
    return [
        KnowledgeFileRead(
            id=str(kb_file.id),
            filename=kb_file.filename,
            file_type=kb_file.file_type,
            status=kb_file.status,
            version=kb_file.version,
            chunk_count=chunk_count,
            created_at=kb_file.created_at,
        )
        for kb_file, chunk_count in rows
    ]


@router.delete("/files/{file_id}", status_code=204, summary="删除知识文件")
async def delete_knowledge_file(
    file_id: uuid_mod.UUID, ws: CurrentWorkspaceQuery, db: DbDep
) -> Response:
    """硬删文件与全部切片（DoD：删除后检索不再命中）。"""
    await _service.delete(db, ws_id=ws.id, file_id=file_id)
    return Response(status_code=204)


@router.post("/search", response_model=KnowledgeSearchResponse, summary="知识检索调试")
async def search_knowledge(
    ws: CurrentWorkspaceQuery, db: DbDep, payload: KnowledgeSearchRequest
) -> KnowledgeSearchResponse:
    """混合检索调试端点（向量 + BM25 → RRF → rerank，与 chat 链路同口径）。"""
    hits = await _service.search(db, ws_id=ws.id, query=payload.query, top_k=payload.top_k)
    return KnowledgeSearchResponse(
        query=payload.query,
        hits=[
            KnowledgeHit(
                chunk_id=str(hit.chunk_id),
                file_id=str(hit.file_id),
                filename=hit.filename,
                chunk_index=hit.chunk_index,
                content=hit.content,
                score=hit.score,
                vector_similarity=hit.vector_similarity,
            )
            for hit in hits
        ],
    )
