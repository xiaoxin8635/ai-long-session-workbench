"""M-03 多会话管理测试：CRUD / 分页 / 归档 / 追加消息 / 并发与乐观锁 / 隔离。"""

import asyncio
import uuid

from httpx import AsyncClient
from sqlalchemy import select

from app.db.session import get_session_factory
from app.models.enums import MemoryStatus
from app.models.memory import Memory

_PASSWORD = "passw0rd123"


async def _setup_user_with_ws(client: AsyncClient, username: str) -> tuple[dict, dict]:
    """注册 + 登录 + 取个人 workspace，返回 (headers, workspace)。"""
    resp = await client.post(
        "/api/auth/register",
        json={"username": username, "password": _PASSWORD},
    )
    assert resp.status_code == 201, resp.text
    resp = await client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
    assert resp.status_code == 200
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    ws_list = (await client.get("/api/workspaces", headers=headers)).json()
    return headers, ws_list[0]


async def _create_session(client: AsyncClient, headers: dict, ws_id: str, title=None) -> dict:
    """创建会话辅助。"""
    resp = await client.post(
        f"/api/sessions?workspace_id={ws_id}", headers=headers, json={"title": title}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _append(
    client: AsyncClient,
    headers: dict,
    ws_id: str,
    sid: str,
    content: str,
    expected_version: int | None = None,
) -> object:
    """追加消息辅助（返回 httpx.Response）。"""
    body = {"role": "user", "content": content}
    if expected_version is not None:
        body["expected_version"] = expected_version
    return await client.post(
        f"/api/sessions/{sid}/messages?workspace_id={ws_id}", headers=headers, json=body
    )


# ---- CRUD 与分页 ----


async def test_session_crud_lifecycle(client: AsyncClient) -> None:
    """创建（默认标题）→ 重命名 → 归档/恢复 → 软删除，状态机全程可查。"""
    headers, ws = await _setup_user_with_ws(client, "crud_user")

    created = await _create_session(client, headers, ws["id"])
    assert created["title"] == "新会话"
    assert created["status"] == "active"
    assert created["version"] == 1

    # 重命名
    resp = await client.patch(
        f"/api/sessions/{created['id']}?workspace_id={ws['id']}",
        headers=headers,
        json={"title": "需求评审"},
    )
    assert resp.status_code == 200 and resp.json()["title"] == "需求评审"

    # 归档后默认列表不可见，include_archived=True 可见
    resp = await client.patch(
        f"/api/sessions/{created['id']}?workspace_id={ws['id']}",
        headers=headers,
        json={"status": "archived"},
    )
    assert resp.status_code == 200 and resp.json()["status"] == "archived"
    visible = (await client.get(f"/api/sessions?workspace_id={ws['id']}", headers=headers)).json()
    assert visible["total"] == 0
    with_archived = (
        await client.get(
            f"/api/sessions?workspace_id={ws['id']}&include_archived=true", headers=headers
        )
    ).json()
    assert with_archived["total"] == 1

    # 恢复 → 再软删除（404 于列表与详情）
    await client.patch(
        f"/api/sessions/{created['id']}?workspace_id={ws['id']}",
        headers=headers,
        json={"status": "active"},
    )
    resp = await client.delete(
        f"/api/sessions/{created['id']}?workspace_id={ws['id']}", headers=headers
    )
    assert resp.status_code == 204
    resp = await client.get(
        f"/api/sessions/{created['id']}?workspace_id={ws['id']}", headers=headers
    )
    assert resp.status_code == 404


async def test_session_list_pagination_and_keyword(client: AsyncClient) -> None:
    """分页（limit/offset/total）与标题关键词过滤。"""
    headers, ws = await _setup_user_with_ws(client, "page_user")
    for i in range(5):
        await _create_session(client, headers, ws["id"], title=f"会话-{i:02d}")

    page1 = (
        await client.get(f"/api/sessions?workspace_id={ws['id']}&limit=2&offset=0", headers=headers)
    ).json()
    assert page1["total"] == 5 and len(page1["items"]) == 2
    page3 = (
        await client.get(f"/api/sessions?workspace_id={ws['id']}&limit=2&offset=4", headers=headers)
    ).json()
    assert len(page3["items"]) == 1

    hit = (
        await client.get(f"/api/sessions?workspace_id={ws['id']}&q=会话-03", headers=headers)
    ).json()
    assert hit["total"] == 1 and hit["items"][0]["title"] == "会话-03"
    miss = (
        await client.get(f"/api/sessions?workspace_id={ws['id']}&q=不存在", headers=headers)
    ).json()
    assert miss["total"] == 0


# ---- 追加消息与 token 统计 ----


async def test_append_message_updates_counters_and_metadata(client: AsyncClient) -> None:
    """追加消息：token 计数、metadata 落库、last_message_at/version 前移。"""
    headers, ws = await _setup_user_with_ws(client, "msg_user")
    session = await _create_session(client, headers, ws["id"], title="统计")

    resp = await _append(
        client,
        headers,
        ws["id"],
        session["id"],
        "你好，世界",
        expected_version=1,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["token_count"] > 0
    assert body["metadata"] == {}

    resp = await _append(
        client,
        headers,
        ws["id"],
        session["id"],
        "hello world from test",
        expected_version=2,
    )
    assert resp.status_code == 201
    messages = (
        await client.get(
            f"/api/sessions/{session['id']}/messages?workspace_id={ws['id']}",
            headers=headers,
        )
    ).json()
    assert messages["total"] == 2
    assert [m["content"][:5] for m in messages["items"]] == ["你好，世界", "hello"]

    detail = (
        await client.get(f"/api/sessions/{session['id']}?workspace_id={ws['id']}", headers=headers)
    ).json()
    assert detail["version"] == 3
    assert detail["last_message_at"] is not None
    # token 累计 = 全部消息 token 之和（计数口径一致性）
    assert detail["token_total"] == sum(m["token_count"] for m in messages["items"])


async def test_append_to_archived_session_409(client: AsyncClient) -> None:
    """归档会话拒绝追加消息（409）。"""
    headers, ws = await _setup_user_with_ws(client, "arch_user")
    session = await _create_session(client, headers, ws["id"])
    await client.patch(
        f"/api/sessions/{session['id']}?workspace_id={ws['id']}",
        headers=headers,
        json={"status": "archived"},
    )
    resp = await _append(client, headers, ws["id"], session["id"], "late message")
    assert resp.status_code == 409


# ---- 乐观锁与并发（DoD：10 并发写无丢失、冲突 409）----


async def test_expected_version_conflict_409(client: AsyncClient) -> None:
    """客户端持有的 expected_version 过期时返回 409。"""
    headers, ws = await _setup_user_with_ws(client, "lock_user")
    session = await _create_session(client, headers, ws["id"])
    resp = await _append(client, headers, ws["id"], session["id"], "first", expected_version=1)
    assert resp.status_code == 201
    # 会话 version 已是 2，仍用过期的 1 → 409
    resp = await _append(client, headers, ws["id"], session["id"], "second", expected_version=1)
    assert resp.status_code == 409


async def test_ten_concurrent_appends_no_loss(client: AsyncClient) -> None:
    """并发 10 写同会话：Redis 锁串行化，全部成功、无丢失、计数准确。"""
    headers, ws = await _setup_user_with_ws(client, "conc_user")
    session = await _create_session(client, headers, ws["id"])

    async def one(i: int) -> int:
        resp = await _append(client, headers, ws["id"], session["id"], f"并发消息 {i}")
        return resp.status_code

    statuses = await asyncio.gather(*(one(i) for i in range(10)))
    assert statuses == [201] * 10

    messages = (
        await client.get(
            f"/api/sessions/{session['id']}/messages?workspace_id={ws['id']}&limit=100",
            headers=headers,
        )
    ).json()
    assert messages["total"] == 10  # 无丢失
    detail = (
        await client.get(f"/api/sessions/{session['id']}?workspace_id={ws['id']}", headers=headers)
    ).json()
    assert detail["version"] == 11  # 10 次前移
    assert detail["token_total"] == sum(m["token_count"] for m in messages["items"])


# ---- 级联归档记忆 ----


async def test_delete_with_cascade_archives_memories(client: AsyncClient) -> None:
    """cascade_memories=true：该会话来源的记忆同步转 archived。"""
    headers, ws = await _setup_user_with_ws(client, "casc_user")
    session = await _create_session(client, headers, ws["id"])

    # 直接落一条来源为该会话的记忆（M-04 管线前的手工构造）
    factory = get_session_factory()
    async with factory() as db:
        db.add(
            Memory(
                workspace_id=uuid.UUID(ws["id"]),
                user_id=uuid.UUID(session["user_id"]),
                memory_type="semantic",
                key="cascade-check",
                content="将被级联归档的记忆",
                confidence=0.9,
                importance=0.8,
                status=MemoryStatus.ACTIVE,
                source_session_id=uuid.UUID(session["id"]),
                source_message_ids=[],
            )
        )
        await db.commit()

    resp = await client.delete(
        f"/api/sessions/{session['id']}?workspace_id={ws['id']}&cascade_memories=true",
        headers=headers,
    )
    assert resp.status_code == 204

    async with factory() as db:
        memory = (
            await db.execute(select(Memory).where(Memory.key == "cascade-check"))
        ).scalar_one()
        assert memory.status == MemoryStatus.ARCHIVED


# ---- 隔离（复用第二道防线）----


async def test_sessions_isolated_between_workspaces(client: AsyncClient) -> None:
    """A 不能读写 B workspace 的会话（详情/消息/追加/删除均 403 或 404）。"""
    headers_a, ws_a = await _setup_user_with_ws(client, "iso_sess_a")
    headers_b, ws_b = await _setup_user_with_ws(client, "iso_sess_b")
    session_a = await _create_session(client, headers_a, ws_a["id"], title="A 的私有会话")

    # B 用自己的 workspace_id 访问 A 的会话 ID → 404（repo 强制 ws 过滤，视同不存在）
    resp = await client.get(
        f"/api/sessions/{session_a['id']}?workspace_id={ws_b['id']}", headers=headers_b
    )
    assert resp.status_code == 404
    resp = await _append(client, headers_b, ws_b["id"], session_a["id"], "越权写入")
    assert resp.status_code == 404
    resp = await client.delete(
        f"/api/sessions/{session_a['id']}?workspace_id={ws_b['id']}", headers=headers_b
    )
    assert resp.status_code == 404

    # B 访问 A 的 workspace_id 本身 → 403（成员校验）
    resp = await client.get(f"/api/sessions?workspace_id={ws_a['id']}", headers=headers_b)
    assert resp.status_code == 403

    # A 的列表不含 B 的会话（列表级隔离）
    list_a = (
        await client.get(f"/api/sessions?workspace_id={ws_a['id']}", headers=headers_a)
    ).json()
    assert list_a["total"] == 1
