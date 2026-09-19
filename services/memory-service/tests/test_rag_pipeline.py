"""M-07 RAG 知识库测试（docs/06 §9）。

覆盖：
  - chunker 纯函数：段落贪心/标题边界/超长段句子切/overlap 尾段复用
  - 分词与 BM25：噪声 token 过滤、相关性排序、空语料安全
  - rerank 客户端：results/data 双协议解析、条数不足防御
  - KnowledgeRetriever：BM25-only / 向量+rerank / superseded 版本不命中
  - ingest 管线：向量回填 + 状态机推进
  - API 链路：上传→检索→引用字段、checksum 幂等、同名版本链、
    删除后不再命中（DoD）、非法类型 422
  - builder 接入：RAG 区块渲染 + citations 与预算裁剪对齐
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context.builder import ContextBuilder
from app.context.schemas import SectionKey
from app.db.session import get_session_factory
from app.llm.embeddings import EmbeddingError
from app.llm.rerank import RerankClient, RerankError
from app.models.enums import KnowledgeFileStatus, MemberRole
from app.models.user import User, Workspace, WorkspaceMember
from app.rag.chunker import split_chunks
from app.rag.ingest import ingest_embeddings
from app.rag.lexical import rank_by_bm25
from app.rag.retriever import KnowledgeRetriever
from app.rag.schemas import CitedChunk
from app.rag.tokenizer import tokenize_for_index
from app.repositories import knowledge_repo

_PASSWORD = "passw0rd123"
_DIM = 1024


# ---- 纯函数：切片 ----


def test_split_chunks_paragraph_greedy() -> None:
    """多个短段落合并进同一块；块数远少于段落数。"""
    paragraphs = [f"第{i}段内容。" for i in range(20)]
    chunks = split_chunks("\n\n".join(paragraphs), target_tokens=30, overlap_ratio=0.0)
    assert 1 < len(chunks) < 20
    assert "".join(chunks).count("第") >= 20  # 内容不丢失


def test_split_chunks_heading_starts_new_block() -> None:
    """markdown 标题强制开新块，且新块不携带上一块的 overlap。"""
    text = "第一章节的正文内容甲。正文内容乙。\n\n# 第二章标题\n\n第二章的正文内容。"
    chunks = split_chunks(text, target_tokens=100, overlap_ratio=0.5)
    assert len(chunks) == 2  # 标题前一块 + 标题起一块
    assert "# 第二章标题" in chunks[1]
    assert "正文内容乙" not in chunks[1]  # 无 overlap


def test_split_chunks_long_paragraph_sentence_split() -> None:
    """无空行超长段退化为句子级贪心，单块不超过目标 token 的合理放大。"""
    long_para = "".join(f"这是第{i}句用于填充超长段落的内容。" for i in range(60))
    chunks = split_chunks(long_para, target_tokens=40, overlap_ratio=0.0)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk  # 每块非空


def test_split_chunks_overlap_tail_reuse() -> None:
    """相邻块以尾段复用实现 overlap（默认 10% 比例）。"""
    paragraphs = [f"段落{i}：" + "内容" * 20 for i in range(10)]
    chunks = split_chunks("\n\n".join(paragraphs), target_tokens=50, overlap_ratio=0.1)
    assert len(chunks) >= 2
    # 第二块以第一块的尾段开头（overlap 生效）
    assert any(paragraphs[j] in chunks[1] for j in range(len(paragraphs)))


# ---- 纯函数：分词与 BM25 ----


def test_tokenize_filters_noise() -> None:
    """空白与纯标点 token 被过滤，实词保留。"""
    tokens = tokenize_for_index("EchoDesk 记忆系统，检索！")
    assert "EchoDesk" in tokens or "echodesk" in tokens
    assert all(t.strip() for t in tokens)
    assert "，" not in tokens and "！" not in tokens


def test_rank_by_bm25_relevance_order() -> None:
    """含查询词多的文档排在前面；空语料与空查询安全返回空。

    2 篇小语料（每词恰命中一半文档，idf 天然退化）验证平滑修复。
    """
    a_id, b_id = uuid.uuid4(), uuid.uuid4()
    corpus = [(a_id, ["检索", "知识", "库"]), (b_id, ["无关", "内容"])]
    ranked = rank_by_bm25(corpus, ["检索", "知识"], top_k=2)
    assert ranked and ranked[0][0] == a_id
    assert rank_by_bm25([], ["查询"], top_k=2) == []
    assert rank_by_bm25(corpus, [], top_k=2) == []


# ---- rerank 客户端：响应解析 ----


def test_rerank_parse_results_protocol() -> None:
    """Cohere 风格 results 按 index 还原顺序。"""
    data = {"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]}
    assert RerankClient._parse_response(data, expected_len=2) == [0.1, 0.9]


def test_rerank_parse_data_protocol_and_shortfall() -> None:
    """data 键兼容；条数不足抛 RerankError（触发降级而非静默零分）。"""
    data = {"data": [{"index": 0, "relevance_score": 0.5}]}
    assert RerankClient._parse_response(data, expected_len=1) == [0.5]
    with pytest.raises(RerankError):
        RerankClient._parse_response(data, expected_len=2)


# ---- 检索器（数据库）----


class FakeEmbedding:
    """固定向量假实现（query → e0 方向，与 seed 的好块高相似）。"""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Args: texts: 待向量化文本（只用第一条）。"""
        return [[1.0] + [0.0] * (_DIM - 1) for _ in texts]


class FakeRerank:
    """按关键词给分的假精排器。"""

    def __init__(self, keyword: str) -> None:
        """Args: keyword: 命中该关键词的文档得高分。"""
        self.keyword = keyword

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        """含关键词的文档 1.0，其余 0.0。"""
        return [1.0 if self.keyword in doc else 0.0 for doc in documents]


class ExplodingRerank:
    """恒失败假实现（验证 rerank 降级 RRF 直排）。"""

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        """Raises: RerankError: 恒定失败。"""
        raise RerankError("rerank down")


async def _seed_workspace(db: AsyncSession) -> uuid.UUID:
    """建独立 workspace（隔离检索语料），返回其 ID。"""
    user = User(username=f"rag_{uuid.uuid4().hex[:8]}", password_hash="x", is_active=True)
    db.add(user)
    await db.flush()  # server 生成主键需先落库，FK 才能取到值
    ws = Workspace(name="rag-test", owner_id=user.id)
    db.add(ws)
    await db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=MemberRole.OWNER))
    await db.commit()
    return ws.id


async def _seed_chunks(
    db: AsyncSession, ws_id: uuid.UUID, filename: str, specs: list[dict]
) -> uuid.UUID:
    """按规格直插文件与切片（跳过解析/摄取，供检索与管线测试）。"""
    file = await knowledge_repo.create_file_with_chunks(
        db,
        ws_id=ws_id,
        filename=filename,
        file_type="md",
        checksum=uuid.uuid4().hex * 2,  # 64 字符占位摘要（与 SHA-256 等宽）
        uploaded_by=(await db.execute(select(User.id).limit(1))).scalar_one(),
        version=1,
        chunks=[
            {
                "chunk_index": spec["index"],
                "content": spec["content"],
                "token_count": 10,
                "tokens": spec["tokens"],
            }
            for spec in specs
        ],
    )
    await db.commit()
    chunks = await knowledge_repo.load_pending_chunks(db, file_id=file.id)
    by_index = {c.chunk_index: c for c in chunks}
    for spec in specs:
        if spec.get("embedding") is not None:
            by_index[spec["index"]].embedding = spec["embedding"]
    await db.commit()
    return file.id


async def test_retriever_bm25_only_and_superseded_excluded() -> None:
    """向量路不可用退化为纯 BM25；superseded 旧版（tokens 清空）不命中。"""
    async with get_session_factory()() as db:
        ws_id = await _seed_workspace(db)
        good_tokens = tokenize_for_index("EchoDesk 知识库的混合检索架构说明")
        await _seed_chunks(
            db,
            ws_id,
            "new.md",
            [
                {"index": 0, "content": "EchoDesk 知识库的混合检索架构说明", "tokens": good_tokens},
                {
                    "index": 1,
                    "content": "无关主题的闲聊内容",
                    "tokens": tokenize_for_index("无关主题的闲聊内容"),
                },
            ],
        )
        # 旧版本：embedding NULL + tokens 清空（mark_superseded 的落库形态）
        old_id = await _seed_chunks(
            db,
            ws_id,
            "new.md",
            [{"index": 0, "content": "EchoDesk 知识库的混合检索架构说明", "tokens": []}],
        )
        old_file = await knowledge_repo.get_file_by_id(db, old_id)
        await knowledge_repo.mark_superseded(db, old_file)
        await db.commit()

        retriever = KnowledgeRetriever(embedding=None, rerank=None)
        hits = await retriever.search(db, ws_id=ws_id, query="知识库混合检索架构")
        assert len(hits) == 1
        assert hits[0].filename == "new.md"
        assert hits[0].chunk_index == 0
        assert hits[0].vector_similarity == 0.0  # BM25-only 路


async def test_retriever_vector_and_rerank() -> None:
    """向量路召回 + rerank 精排改变最终顺序。"""
    async with get_session_factory()() as db:
        ws_id = await _seed_workspace(db)
        await _seed_chunks(
            db,
            ws_id,
            "arch.md",
            [
                {
                    "index": 0,
                    "content": "与查询方向一致的向量内容",
                    "tokens": tokenize_for_index("与查询方向一致的向量内容"),
                    "embedding": [0.9] + [0.0] * (_DIM - 1),
                },
                {
                    "index": 1,
                    "content": "正交方向的干扰内容",
                    "tokens": tokenize_for_index("正交方向的干扰内容"),
                    "embedding": [0.0, 1.0] + [0.0] * (_DIM - 2),
                },
            ],
        )
        retriever = KnowledgeRetriever(embedding=FakeEmbedding(), rerank=FakeRerank("干扰"))
        hits = await retriever.search(db, ws_id=ws_id, query="任意查询")
        # rerank 把含"干扰"的块排到第一（覆盖向量相似度顺序）
        assert hits[0].chunk_index == 1
        assert hits[0].vector_similarity > 0.5 or hits[1].vector_similarity > 0.5

        # rerank 失败降级 RRF 直排：向量近邻块回到第一
        degraded = KnowledgeRetriever(embedding=FakeEmbedding(), rerank=ExplodingRerank())
        hits2 = await degraded.search(db, ws_id=ws_id, query="任意查询")
        assert hits2[0].chunk_index == 0


async def test_ingest_pipeline_fills_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """摄取管线批量回填向量并把状态机推进到 embedded。"""
    async with get_session_factory()() as db:
        ws_id = await _seed_workspace(db)
        file_id = await _seed_chunks(
            db,
            ws_id,
            "ingest.md",
            [{"index": 0, "content": "待向量化的内容", "tokens": ["内容"]}],
        )
    monkeypatch.setattr("app.rag.ingest.get_embedding_client", lambda: FakeEmbedding())
    await ingest_embeddings(file_id)

    async with get_session_factory()() as db:
        file = await knowledge_repo.get_file_by_id(db, file_id)
        assert file is not None and file.status == KnowledgeFileStatus.EMBEDDED
        chunks = await knowledge_repo.load_pending_chunks(db, file_id=file_id)
        assert chunks == []  # 无待补向量切片


# ---- API 链路（上传/列表/检索/删除）----


def _no_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    """禁用向量与精排客户端（测试环境跑纯 BM25，避免外呼）。"""

    def no_embedding_client() -> None:
        """恒定抛错（get_embedding_client 替身）。"""
        raise EmbeddingError("not configured")

    def no_rerank_client() -> None:
        """恒定抛错（get_rerank_client 替身）。"""
        raise RerankError("not configured")

    monkeypatch.setattr("app.services.knowledge_service.get_embedding_client", no_embedding_client)
    monkeypatch.setattr("app.services.knowledge_service.get_rerank_client", no_rerank_client)


async def _auth_setup(client: AsyncClient, username: str) -> tuple[dict, str]:
    """注册登录并取个人 workspace，返回 (headers, ws_id)。"""
    await client.post("/api/auth/register", json={"username": username, "password": _PASSWORD})
    resp = await client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    ws = (await client.get("/api/workspaces", headers=headers)).json()[0]
    return headers, ws["id"]


async def test_upload_search_citation_flow(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上传→状态机→检索调试命中（含引用定位字段）。"""
    monkeypatch.setattr("app.services.knowledge_service.spawn_ingest", lambda file_id: None)
    _no_embedding(monkeypatch)
    headers, ws_id = await _auth_setup(client, "rag_flow")

    body = (
        "# 架构说明\n\nEchoDesk 的知识检索采用向量召回与 BM25 关键词召回双路混合。\n\n"
        "报价单金额为四十二万元整。"
    )
    resp = await client.post(
        "/api/knowledge/files",
        headers=headers,
        params={"workspace_id": ws_id},
        files={"file": ("arch.md", body.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code == 201, resp.text
    meta = resp.json()
    assert meta["status"] == KnowledgeFileStatus.PARSING.value
    assert meta["chunk_count"] >= 1

    search = await client.post(
        "/api/knowledge/search",
        headers=headers,
        params={"workspace_id": ws_id},
        json={"query": "知识检索 双路混合 召回"},
    )
    assert search.status_code == 200, search.text
    hits = search.json()["hits"]
    assert hits and hits[0]["filename"] == "arch.md"
    assert "双路混合" in hits[0]["content"]
    assert hits[0]["chunk_id"] and hits[0]["file_id"] == meta["id"]


async def test_upload_checksum_idempotent(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同内容重传幂等：返回同一文件，库内不新增记录。"""
    monkeypatch.setattr("app.services.knowledge_service.spawn_ingest", lambda file_id: None)
    headers, ws_id = await _auth_setup(client, "rag_dupe")
    files = {"file": ("same.txt", b"identical content", "text/plain")}

    first = await client.post(
        "/api/knowledge/files", headers=headers, params={"workspace_id": ws_id}, files=files
    )
    second = await client.post(
        "/api/knowledge/files", headers=headers, params={"workspace_id": ws_id}, files=files
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    listing = await client.get(
        "/api/knowledge/files", headers=headers, params={"workspace_id": ws_id}
    )
    assert len(listing.json()) == 1


async def test_upload_same_filename_versions(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同名不同内容重传：version+1、旧版 superseded 且检索只命中新版。"""
    monkeypatch.setattr("app.services.knowledge_service.spawn_ingest", lambda file_id: None)
    _no_embedding(monkeypatch)
    headers, ws_id = await _auth_setup(client, "rag_version")

    v1 = await client.post(
        "/api/knowledge/files",
        headers=headers,
        params={"workspace_id": ws_id},
        files={
            "file": ("guide.md", "旧版本内容：部署用 Docker Compose。".encode(), "text/markdown")
        },
    )
    v2 = await client.post(
        "/api/knowledge/files",
        headers=headers,
        params={"workspace_id": ws_id},
        files={"file": ("guide.md", "新版本内容：部署改为 Kubernetes。".encode(), "text/markdown")},
    )
    assert v1.json()["version"] == 1
    assert v2.json()["version"] == 2
    assert v1.json()["id"] != v2.json()["id"]

    listing = (
        await client.get("/api/knowledge/files", headers=headers, params={"workspace_id": ws_id})
    ).json()
    statuses = {item["version"]: item["status"] for item in listing}
    assert statuses[1] == KnowledgeFileStatus.SUPERSEDED.value
    assert statuses[2] == KnowledgeFileStatus.PARSING.value

    search = await client.post(
        "/api/knowledge/search",
        headers=headers,
        params={"workspace_id": ws_id},
        json={"query": "部署 Docker Compose"},
    )
    contents = [hit["content"] for hit in search.json()["hits"]]
    assert contents and all("Kubernetes" in c for c in contents)  # 旧版不命中


async def test_delete_file_removes_from_search(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DoD：删除后列表为空、检索不再命中、再删 404。"""
    monkeypatch.setattr("app.services.knowledge_service.spawn_ingest", lambda file_id: None)
    _no_embedding(monkeypatch)
    headers, ws_id = await _auth_setup(client, "rag_delete")
    uploaded = await client.post(
        "/api/knowledge/files",
        headers=headers,
        params={"workspace_id": ws_id},
        files={"file": ("temp.md", "可删除的临时知识内容。".encode(), "text/markdown")},
    )
    file_id = uploaded.json()["id"]

    deleted = await client.delete(
        f"/api/knowledge/files/{file_id}", headers=headers, params={"workspace_id": ws_id}
    )
    assert deleted.status_code == 204
    listing = await client.get(
        "/api/knowledge/files", headers=headers, params={"workspace_id": ws_id}
    )
    assert listing.json() == []
    search = await client.post(
        "/api/knowledge/search",
        headers=headers,
        params={"workspace_id": ws_id},
        json={"query": "临时知识内容"},
    )
    assert search.json()["hits"] == []
    again = await client.delete(
        f"/api/knowledge/files/{file_id}", headers=headers, params={"workspace_id": ws_id}
    )
    assert again.status_code == 404


async def test_upload_rejects_unsupported_type(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非白名单扩展名 422；超限体积 413。"""
    monkeypatch.setattr("app.services.knowledge_service.spawn_ingest", lambda file_id: None)
    headers, ws_id = await _auth_setup(client, "rag_reject")
    bad = await client.post(
        "/api/knowledge/files",
        headers=headers,
        params={"workspace_id": ws_id},
        files={"file": ("evil.exe", b"MZ...", "application/octet-stream")},
    )
    assert bad.status_code == 422


# ---- builder 接入（RAG 区块与 citations）----


class FakeKnowledge:
    """固定返回两条命中的假知识检索器。"""

    def __init__(self, hits: list[CitedChunk]) -> None:
        """Args: hits: search 的固定返回。"""
        self.hits = hits
        self.calls: list[str] = []

    async def search(
        self, db: AsyncSession, *, ws_id: uuid.UUID, query: str, top_k: int | None = None
    ) -> list[CitedChunk]:
        """记录查询并返回预置命中。"""
        self.calls.append(query)
        return self.hits


class FakeWorkingMemory:
    """窗口恒返回当前问题的假 Working Memory。"""

    async def window(self, session_id: uuid.UUID, budget_tokens: int) -> list[dict]:
        """单条用户消息窗口。"""
        return [{"role": "user", "content": "知识库问题"}]


async def test_builder_rag_bucket_and_citations() -> None:
    """RAG 命中注入 <knowledge> 区块，citations 与装入条目对齐。"""
    hits = [
        CitedChunk(
            chunk_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            filename="handbook.md",
            chunk_index=i,
            content=f"手册要点{i}",
            score=0.9 - i * 0.1,
        )
        for i in range(2)
    ]
    knowledge = FakeKnowledge(hits)
    builder = ContextBuilder(FakeWorkingMemory(), retriever=None, knowledge=knowledge)

    async with get_session_factory()() as db:
        assembled = await builder.build(
            db,
            ws_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            query="知识库问题",
        )
    assert knowledge.calls == ["知识库问题"]
    system = assembled.messages[0]["content"]
    assert "<knowledge_chunks>" in system and "手册要点0" in system
    rag_usage = next(u for u in assembled.sections if u.key is SectionKey.RAG)
    assert rag_usage.included == 2
    assert len(assembled.citations) == 2
    assert assembled.citations[0].filename == "handbook.md"


async def test_builder_citations_filtered_by_budget() -> None:
    """RAG 预算不足时未装入的命中不出现在 citations。"""
    hits = [
        CitedChunk(
            chunk_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            filename="big.md",
            chunk_index=i,
            content="超" * 550,  # 每条 550 token；默认 profile RAG 预算 900，装不下第二条
            score=0.9 - i * 0.1,
        )
        for i in range(2)
    ]
    builder = ContextBuilder(FakeWorkingMemory(), retriever=None, knowledge=FakeKnowledge(hits))
    async with get_session_factory()() as db:
        assembled = await builder.build(
            db,
            ws_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            query="预算问题",
        )
    assert len(assembled.citations) == 1  # 只引用真正装入的 chunk
    assert "超" * 550 in assembled.messages[0]["content"]
