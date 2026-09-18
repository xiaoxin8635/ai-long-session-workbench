"""M-02 workspace 隔离测试套件（长期回归基线，docs/06 §M-02）。

三条核心用例：
  1. 用户 A 访问用户 B 的 workspace → 403（成员校验）
  2. 伪造 / 过期 / 缺失令牌 → 401
  3. MEMBER 角色访问 ADMIN 端点 → 403（RBAC）
另含正向路径：owner 添加成员后，成员可访问详情。
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt as pyjwt
from httpx import AsyncClient

from app.core.config import get_settings

_PASSWORD = "passw0rd123"


async def _register_and_login(client: AsyncClient, username: str) -> dict:
    """注册 + 登录，返回 TokenPair。"""
    resp = await client.post(
        "/api/auth/register",
        json={"username": username, "password": _PASSWORD, "display_name": username.title()},
    )
    assert resp.status_code == 201, resp.text
    resp = await client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _my_workspaces(client: AsyncClient, tokens: dict) -> list[dict]:
    """带认证列出当前用户 workspace。"""
    resp = await client.get(
        "/api/workspaces", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _expired_access_token(user_id: str) -> str:
    """手工签发一个已过期的 access 令牌（过期路径回归用）。"""
    settings = get_settings()
    now = datetime.now(UTC) - timedelta(hours=1)
    payload = {"sub": user_id, "type": "access", "iat": now, "exp": now}
    return pyjwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


# ---- 用例 1：跨用户隔离（A 不能访问 B 的 workspace）----


async def test_user_cannot_access_others_workspace(client: AsyncClient) -> None:
    """A 访问 B 的个人 workspace：详情/成员/添加成员全部 403。"""
    alice = await _register_and_login(client, "iso_alice")
    bob = await _register_and_login(client, "iso_bob")
    bob_ws = (await _my_workspaces(client, bob))[0]

    headers = {"Authorization": f"Bearer {alice['access_token']}"}
    assert (await client.get(f"/api/workspaces/{bob_ws['id']}", headers=headers)).status_code == 403
    assert (
        await client.get(f"/api/workspaces/{bob_ws['id']}/members", headers=headers)
    ).status_code == 403
    resp = await client.post(
        f"/api/workspaces/{bob_ws['id']}/members",
        json={"username": "iso_alice"},
        headers=headers,
    )
    assert resp.status_code == 403


async def test_workspace_list_only_shows_own(client: AsyncClient) -> None:
    """A 的 workspace 列表不含 B 的 workspace（列表级隔离）。"""
    await _register_and_login(client, "list_alice")
    bob = await _register_and_login(client, "list_bob")
    bob_ws_id = (await _my_workspaces(client, bob))[0]["id"]

    alice = await _login_again(client, "list_alice")
    visible = {ws["id"] for ws in await _my_workspaces(client, alice)}
    assert bob_ws_id not in visible


async def _login_again(client: AsyncClient, username: str) -> dict:
    """重复登录辅助（隔离测试内复用）。"""
    resp = await client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---- 用例 2：令牌校验（伪造 / 过期 / 缺失）----


async def test_missing_token_401(client: AsyncClient) -> None:
    """未携带 Bearer 令牌访问受保护端点 → 401。"""
    assert (await client.get("/api/workspaces")).status_code == 401


async def test_garbage_token_401(client: AsyncClient) -> None:
    """伪造令牌 → 401。"""
    resp = await client.get("/api/workspaces", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


async def test_expired_token_401(client: AsyncClient) -> None:
    """过期 access 令牌 → 401（exp 校验回归）。"""
    tokens = await _register_and_login(client, "expired_user")
    # 先拿到真实 user_id（me 端点），再签发过期令牌
    me = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    user_id = me.json()["id"]
    expired = _expired_access_token(user_id)
    resp = await client.get("/api/workspaces", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401


# ---- 用例 3：RBAC（MEMBER 不能访问 ADMIN 端点）----


async def test_member_role_blocked_on_admin_endpoints(client: AsyncClient) -> None:
    """MEMBER 访问成员列表/添加成员 → 403；owner/ADMIN 可访问。"""
    owner = await _register_and_login(client, "rbac_owner")
    ws_id = (await _my_workspaces(client, owner))[0]["id"]
    owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}

    # 准备一个 MEMBER 用户并加入 workspace
    await _register_and_login(client, "rbac_member")
    resp = await client.post(
        f"/api/workspaces/{ws_id}/members",
        json={"username": "rbac_member", "role": "member"},
        headers=owner_headers,
    )
    assert resp.status_code == 201, resp.text

    member = await _login_again(client, "rbac_member")
    member_headers = {"Authorization": f"Bearer {member['access_token']}"}

    # MEMBER：可见详情（成员资格通过）但成员管理被拒（RBAC）
    assert (await client.get(f"/api/workspaces/{ws_id}", headers=member_headers)).status_code == 200
    assert (
        await client.get(f"/api/workspaces/{ws_id}/members", headers=member_headers)
    ).status_code == 403
    assert (
        await client.post(
            f"/api/workspaces/{ws_id}/members",
            json={"username": "rbac_owner"},
            headers=member_headers,
        )
    ).status_code == 403

    # OWNER：成员列表可见且包含自己与 member
    resp = await client.get(f"/api/workspaces/{ws_id}/members", headers=owner_headers)
    assert resp.status_code == 200
    roles = {m["username"]: m["role"] for m in resp.json()}
    assert roles["rbac_owner"] == "owner"
    assert roles["rbac_member"] == "member"


async def test_member_cannot_promote_self_to_owner(client: AsyncClient) -> None:
    """任何经此接口指定 OWNER 角色都被拒绝（403），即使操作者是 ADMIN。"""
    owner = await _register_and_login(client, "esc_owner")
    ws_id = (await _my_workspaces(client, owner))[0]["id"]
    await _register_and_login(client, "esc_admin")
    resp = await client.post(
        f"/api/workspaces/{ws_id}/members",
        json={"username": "esc_admin", "role": "admin"},
        headers={"Authorization": f"Bearer {owner['access_token']}"},
    )
    assert resp.status_code == 201

    admin = await _login_again(client, "esc_admin")
    resp = await client.post(
        f"/api/workspaces/{ws_id}/members",
        json={"username": "esc_admin", "role": "owner"},
        headers={"Authorization": f"Bearer {admin['access_token']}"},
    )
    assert resp.status_code == 403


async def test_nonexistent_workspace_403_not_404(client: AsyncClient) -> None:
    """随机 ws_id 返回 403 而非 404（避免 workspace 存在性泄露）。"""
    tokens = await _register_and_login(client, "enum_guard")
    resp = await client.get(
        f"/api/workspaces/{uuid4()}",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert resp.status_code == 403
