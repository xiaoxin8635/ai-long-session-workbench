"""M-11 记忆管理 API 测试（docs/06 §13 / docs/05 §7.1 面板）。

覆盖：
  - 列表：type/status/关键词过滤、分页、conflicted 置顶、软删除排除
  - 详情：版本链（supersedes 上溯）+ 事件流水
  - 编辑：部分更新 version+1、edit_by_user 审计（source=user）、expires_at null 清除 TTL
  - 软删除：列表不再可见；幂等
  - 冲突裁决：keep=this/other 双向、版本链挂接、非 conflicted / 无对手方拒绝
  - 检索测试：fake embedding 命中打分、未配置降级空列表
  - 鉴权：401 / 403 跨 workspace / 404；Pydantic 输入防线
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.db.session import get_session_factory
from app.llm.embeddings import EmbeddingError
from app.models.enums import MemoryEventSource, MemoryEventType, MemoryStatus, MemoryType
from app.models.memory import Memory, MemoryEvent
from app.repositories import memory_repo, user_repo
from app.schemas.memory import MemoryUpdate

_PASSWORD = "passw0rd123"


class FakeEmbedding:
    """固定向量假实现：任何文本都映射到同一向量（与已存向量相似度 1.0）。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Args: texts: 待向量化文本列表。Returns: 每条文本的固定 1024 维向量。"""
        self.calls.append(texts)
        return [[0.1] * 1024 for _ in texts]


async def _auth_setup(client: AsyncClient, username: str) -> tuple[dict, dict]:
    """注册登录并取个人 workspace，返回 (headers, ws)。"""
    await client.post("/api/auth/register", json={"username": username, "password": _PASSWORD})
    resp = await client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    ws = (await client.get("/api/workspaces", headers=headers)).json()[0]
    return headers, ws


async def _db_of(username: str) -> tuple[Any, uuid.UUID]:
    """取 db 会话与该 API 用户的 user_id（直写记忆用，workspace 由 API 层获取）。

    内部异常时自行 close 防连接泄漏（残留 idle in transaction 会阻塞后续用例建表）。
    """
    db = get_session_factory()()
    try:
        user = await user_repo.get_by_username(db, username)
        assert user is not None
        return db, user.id
    except BaseException:
        await db.close()
        raise


async def _seed_memory(
    db: Any,
    *,
    ws_id: uuid.UUID,
    user_id: uuid.UUID,
    key: str,
    content: str,
    memory_type: MemoryType = MemoryType.SEMANTIC,
    expires_at: datetime | None = None,
    supersedes_id: uuid.UUID | None = None,
    embedding: list[float] | None = None,
) -> Memory:
    """直写一条 active 记忆（含 CREATED 事件）并提交。"""
    memory = await memory_repo.create_memory(
        db,
        ws_id=ws_id,
        user_id=user_id,
        memory_type=memory_type,
        key=key,
        content=content,
        confidence=0.8,
        importance=0.6,
        expires_at=expires_at,
        supersedes_id=supersedes_id,
        embedding=embedding,
    )
    await db.commit()
    return memory


def _ws_url(ws_id: str, memory_id: str | None = None) -> str:
    """构造带 workspace 过滤的记忆 URL（可带路径 id 段）。"""
    base = "/api/memories"
    if memory_id is not None:
        base += f"/{memory_id}"
    return f"{base}?workspace_id={ws_id}"


async def test_list_filters_pagination_conflicted_first(client: AsyncClient) -> None:
    """列表：type/status/关键词过滤、分页 total、conflicted 置顶、软删除排除。"""
    headers, ws = await _auth_setup(client, "mem_list")
    db, user_id = await _db_of("mem_list")
    try:
        ws_id = uuid.UUID(ws["id"])
        await _seed_memory(
            db, ws_id=ws_id, user_id=user_id, key="profile.city", content="用户住在杭州"
        )
        conflicted = await _seed_memory(
            db, ws_id=ws_id, user_id=user_id, key="skill.rust", content="用户熟悉 Rust"
        )
        conflicted.status = MemoryStatus.CONFLICTED
        deleted = await _seed_memory(
            db, ws_id=ws_id, user_id=user_id, key="gone.one", content="待删除条目"
        )
        await memory_repo.soft_delete(db, deleted)
        await db.commit()
        base = _ws_url(ws["id"])

        resp = (await client.get(base, headers=headers)).json()
        assert resp["total"] == 2  # 软删除排除
        assert [m["key"] for m in resp["items"]] == [
            "skill.rust",
            "profile.city",
        ]  # conflicted 置顶

        by_type = (await client.get(f"{base}&memory_type=semantic", headers=headers)).json()
        assert by_type["total"] == 2
        by_status = (await client.get(f"{base}&status=conflicted", headers=headers)).json()
        assert [m["key"] for m in by_status["items"]] == ["skill.rust"]
        by_kw = (await client.get(f"{base}&q=杭州", headers=headers)).json()
        assert [m["key"] for m in by_kw["items"]] == ["profile.city"]
        paged = (await client.get(f"{base}&limit=1&offset=1", headers=headers)).json()
        assert paged["total"] == 2 and len(paged["items"]) == 1
        assert paged["items"][0]["key"] == "profile.city"
    finally:
        await db.close()


async def test_detail_version_chain_and_events(client: AsyncClient) -> None:
    """详情：版本链含被替代旧版，事件流水留痕。"""
    headers, ws = await _auth_setup(client, "mem_detail")
    db, user_id = await _db_of("mem_detail")
    try:
        ws_id = uuid.UUID(ws["id"])
        v1 = await _seed_memory(
            db, ws_id=ws_id, user_id=user_id, key="job.progress", content="简历投递中"
        )
        v2 = await _seed_memory(
            db,
            ws_id=ws_id,
            user_id=user_id,
            key="job.progress",
            content="已约一面",
            supersedes_id=v1.id,
        )
        await memory_repo.mark_superseded(db, v1, by_id=v2.id)
        await db.commit()

        resp = await client.get(_ws_url(ws["id"], str(v2.id)), headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"] == "job.progress"
        assert data["status"] == MemoryStatus.ACTIVE
        assert [m["id"] for m in data["version_chain"]] == [str(v1.id)]
        assert {e["event_type"] for e in data["events"]} >= {MemoryEventType.CREATED}
    finally:
        await db.close()


async def test_patch_updates_version_and_audits(client: AsyncClient) -> None:
    """编辑：部分更新 version+1、edit_by_user 审计 source=user、expires_at null 清 TTL。"""
    headers, ws = await _auth_setup(client, "mem_edit")
    db, user_id = await _db_of("mem_edit")
    try:
        ws_id = uuid.UUID(ws["id"])
        memory = await _seed_memory(
            db,
            ws_id=ws_id,
            user_id=user_id,
            key="pref.style",
            content="先给结论",
            memory_type=MemoryType.PROCEDURAL,
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        resp = await client.patch(
            _ws_url(ws["id"], str(memory.id)),
            headers=headers,
            json={"content": "先给结论再给细节", "confidence": 0.95, "expires_at": None},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["content"] == "先给结论再给细节"
        assert data["confidence"] == 0.95
        assert data["version"] == 2
        assert data["expires_at"] is None  # 显式 null 清除 TTL

        events = (
            (
                await db.execute(
                    select(MemoryEvent)
                    .where(MemoryEvent.memory_id == memory.id)
                    .order_by(MemoryEvent.created_at)
                )
            )
            .scalars()
            .all()
        )
        edit_events = [e for e in events if e.event_type == MemoryEventType.EDITED_BY_USER]
        assert len(edit_events) == 1
        assert edit_events[0].source == MemoryEventSource.USER
        assert edit_events[0].old_value == "先给结论"
    finally:
        await db.close()


async def test_patch_no_change_no_version_bump(client: AsyncClient) -> None:
    """编辑空提交（无字段变化）：version 不动、不落审计。"""
    headers, ws = await _auth_setup(client, "mem_noop")
    db, user_id = await _db_of("mem_noop")
    try:
        memory = await _seed_memory(
            db, ws_id=uuid.UUID(ws["id"]), user_id=user_id, key="stable.fact", content="稳定事实"
        )
        resp = await client.patch(_ws_url(ws["id"], str(memory.id)), headers=headers, json={})
        assert resp.status_code == 200
        assert resp.json()["version"] == 1
        events = (
            await db.execute(select(MemoryEvent).where(MemoryEvent.memory_id == memory.id))
        ).scalars()
        assert all(e.event_type != MemoryEventType.EDITED_BY_USER for e in events)
    finally:
        await db.close()


async def test_delete_soft_hides_from_list(client: AsyncClient) -> None:
    """软删除：204；列表不再可见；幂等重复删除仍 204。"""
    headers, ws = await _auth_setup(client, "mem_del")
    db, user_id = await _db_of("mem_del")
    try:
        memory = await _seed_memory(
            db, ws_id=uuid.UUID(ws["id"]), user_id=user_id, key="temp.note", content="临时笔记"
        )
        first = await client.delete(_ws_url(ws["id"], str(memory.id)), headers=headers)
        assert first.status_code == 204
        again = await client.delete(_ws_url(ws["id"], str(memory.id)), headers=headers)
        assert again.status_code == 204  # 幂等
        listing = (await client.get(_ws_url(ws["id"]), headers=headers)).json()
        assert listing["total"] == 0
    finally:
        await db.close()


async def _seed_conflict_pair(
    db: Any, ws_id: uuid.UUID, user_id: uuid.UUID, key: str
) -> tuple[Memory, Memory]:
    """造一对同 key 的 conflicted 记忆（模拟抽取管线 COEXIST 结果）。"""
    old = await _seed_memory(db, ws_id=ws_id, user_id=user_id, key=key, content="旧说法")
    new = await _seed_memory(db, ws_id=ws_id, user_id=user_id, key=key, content="新说法")
    await memory_repo.mark_conflicted(db, old)
    await memory_repo.mark_conflicted(db, new)
    await db.commit()
    return old, new


async def test_resolve_keep_other(client: AsyncClient) -> None:
    """裁决 keep=other：对手方回 active、本条 superseded 并挂版本链。"""
    headers, ws = await _auth_setup(client, "mem_res_a")
    db, user_id = await _db_of("mem_res_a")
    try:
        old, new = await _seed_conflict_pair(db, uuid.UUID(ws["id"]), user_id, "skill.frontend")
        resp = await client.post(
            f"/api/memories/{new.id}/resolve?workspace_id={ws['id']}",
            headers=headers,
            json={"keep": "other"},
        )
        assert resp.status_code == 200, resp.text
        winner = resp.json()
        assert winner["id"] == str(old.id)
        assert winner["status"] == MemoryStatus.ACTIVE

        await db.refresh(new)
        await db.refresh(old)
        assert new.status == MemoryStatus.SUPERSEDED
        assert old.supersedes_id == new.id  # 版本链：保留者指向被放弃者

        sup_events = (
            (
                await db.execute(
                    select(MemoryEvent).where(
                        MemoryEvent.memory_id == new.id,
                        MemoryEvent.event_type == MemoryEventType.SUPERSEDED,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert sup_events and sup_events[0].source == MemoryEventSource.USER
    finally:
        await db.close()


async def test_resolve_keep_this(client: AsyncClient) -> None:
    """裁决 keep=this：本条保留，对手方 superseded。"""
    headers, ws = await _auth_setup(client, "mem_res_b")
    db, user_id = await _db_of("mem_res_b")
    try:
        old, new = await _seed_conflict_pair(db, uuid.UUID(ws["id"]), user_id, "skill.backend")
        resp = await client.post(
            f"/api/memories/{new.id}/resolve?workspace_id={ws['id']}",
            headers=headers,
            json={"keep": "this"},
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == str(new.id)
        await db.refresh(old)
        assert old.status == MemoryStatus.SUPERSEDED
    finally:
        await db.close()


async def test_resolve_invalid_targets(client: AsyncClient) -> None:
    """裁决防线：active 条目 422；孤立 conflicted（无对手方）422；不存在 404。"""
    headers, ws = await _auth_setup(client, "mem_res_c")
    db, user_id = await _db_of("mem_res_c")
    try:
        ws_id = uuid.UUID(ws["id"])
        active = await _seed_memory(
            db, ws_id=ws_id, user_id=user_id, key="normal.fact", content="正常条目"
        )
        lonely = await _seed_memory(
            db, ws_id=ws_id, user_id=user_id, key="lonely.conflict", content="孤立冲突"
        )
        lonely.status = MemoryStatus.CONFLICTED
        await db.commit()

        not_conflicted = await client.post(
            f"/api/memories/{active.id}/resolve?workspace_id={ws['id']}",
            headers=headers,
            json={"keep": "this"},
        )
        assert not_conflicted.status_code == 422
        no_peer = await client.post(
            f"/api/memories/{lonely.id}/resolve?workspace_id={ws['id']}",
            headers=headers,
            json={"keep": "this"},
        )
        assert no_peer.status_code == 422
        missing = await client.post(
            f"/api/memories/{uuid.uuid4()}/resolve?workspace_id={ws['id']}",
            headers=headers,
            json={"keep": "this"},
        )
        assert missing.status_code == 404
    finally:
        await db.close()


async def test_search_with_fake_embedding(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """检索测试：注入 fake embedding 的服务实例，命中含 score/similarity。"""
    from app.api.routes import memories as memories_route
    from app.services.memory_admin_service import MemoryAdminService

    headers, ws = await _auth_setup(client, "mem_search")
    db, user_id = await _db_of("mem_search")
    try:
        fake = FakeEmbedding()
        monkeypatch.setattr(memories_route, "_service", MemoryAdminService(embedding=fake))  # type: ignore[arg-type]
        # 记忆向量与 fake 输出同值 → 余弦相似度 1.0
        await _seed_memory(
            db,
            ws_id=uuid.UUID(ws["id"]),
            user_id=user_id,
            key="profile.spice",
            content="用户偏好微辣",
            embedding=[0.1] * 1024,
        )

        resp = await client.post(
            f"/api/memories/search?workspace_id={ws['id']}",
            headers=headers,
            json={"query": "口味偏好", "top_k": 5},
        )
        assert resp.status_code == 200, resp.text
        hits = resp.json()
        assert fake.calls  # embedding 被调用
        assert len(hits) == 1
        assert hits[0]["key"] == "profile.spice"
        assert hits[0]["similarity"] == pytest.approx(1.0, abs=1e-6)
        assert hits[0]["score"] > 0
    finally:
        await db.close()


async def test_search_degrades_without_embedding(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """检索测试降级：embedding 未配置返回空列表（200，不 5xx）。"""

    def _no_embedding() -> None:
        raise EmbeddingError("embedding 未配置（测试注入）")

    from app.services import memory_admin_service as svc_mod

    monkeypatch.setattr(svc_mod, "get_embedding_client", _no_embedding)
    headers, ws = await _auth_setup(client, "mem_noemb")
    resp = await client.post(
        f"/api/memories/search?workspace_id={ws['id']}",
        headers=headers,
        json={"query": "任意"},
    )
    assert resp.status_code == 200
    assert resp.json() == []


async def test_auth_guards(client: AsyncClient) -> None:
    """鉴权防线：401 无令牌 / 403 跨 workspace / 404 不存在。"""
    headers, ws = await _auth_setup(client, "mem_guard")
    other_headers, _ = await _auth_setup(client, "mem_other")

    no_token = await client.get(_ws_url(ws["id"]))
    assert no_token.status_code == 401

    cross = await client.get(_ws_url(ws["id"]), headers=other_headers)
    assert cross.status_code == 403

    missing = await client.get(_ws_url(ws["id"], str(uuid.uuid4())), headers=headers)
    assert missing.status_code == 404


def test_memory_update_schema_rejects_bad_input() -> None:
    """Pydantic 防线：confidence 越界 / content 空串被拒。"""
    with pytest.raises(ValueError):
        MemoryUpdate(confidence=1.5)
    with pytest.raises(ValueError):
        MemoryUpdate(content="")
