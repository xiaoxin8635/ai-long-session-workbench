"""M-02 认证流程测试：注册 / 登录 / 刷新 / me（真实 PG，workbench_test 库）。"""

from httpx import AsyncClient


async def _register(client: AsyncClient, username: str, password: str = "passw0rd123") -> dict:
    """注册辅助：返回响应 JSON。"""
    resp = await client.post(
        "/api/auth/register",
        json={"username": username, "password": password, "display_name": username.title()},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _login(client: AsyncClient, username: str, password: str = "passw0rd123") -> dict:
    """登录辅助：返回 TokenPair JSON。"""
    resp = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_register_creates_user_and_personal_workspace(client: AsyncClient) -> None:
    """注册 201：返回用户信息，且自动创建个人 workspace 并绑 owner。"""
    body = await _register(client, "alice")
    assert body["username"] == "alice"
    assert "password" not in body and "password_hash" not in body

    tokens = await _login(client, "alice")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    resp = await client.get("/api/workspaces", headers=headers)
    assert resp.status_code == 200
    workspaces = resp.json()
    assert len(workspaces) == 1
    assert workspaces[0]["name"] == "Alice 的空间"
    assert workspaces[0]["owner_id"] == body["id"]


async def test_register_duplicate_username_conflict(client: AsyncClient) -> None:
    """同名注册返回 409（防重复账号）。"""
    await _register(client, "bob")
    resp = await client.post(
        "/api/auth/register", json={"username": "bob", "password": "passw0rd123"}
    )
    assert resp.status_code == 409
    assert resp.json()["title"] == "conflict"


async def test_register_validation_errors(client: AsyncClient) -> None:
    """非法输入返回 422：用户名含非法字符 / 密码过短。"""
    resp = await client.post(
        "/api/auth/register", json={"username": "非法用户", "password": "passw0rd123"}
    )
    assert resp.status_code == 422
    resp = await client.post(
        "/api/auth/register", json={"username": "carol", "password": "short"}
    )
    assert resp.status_code == 422


async def test_login_issues_token_pair_and_me(client: AsyncClient) -> None:
    """登录签发双令牌；access 令牌可访问 /api/auth/me。"""
    await _register(client, "dave")
    tokens = await _login(client, "dave")
    assert tokens["token_type"] == "bearer"
    assert tokens["access_token"] and tokens["refresh_token"]

    resp = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert resp.status_code == 200
    assert resp.json()["username"] == "dave"


async def test_login_wrong_password_uniform_401(client: AsyncClient) -> None:
    """密码错误与用户不存在均返回同一 401（防用户枚举）。"""
    await _register(client, "erin")
    wrong_pw = await client.post(
        "/api/auth/login", json={"username": "erin", "password": "totally-wrong"}
    )
    no_user = await client.post(
        "/api/auth/login", json={"username": "ghost-user", "password": "whatever1"}
    )
    assert wrong_pw.status_code == 401
    assert no_user.status_code == 401
    assert wrong_pw.json()["detail"] == no_user.json()["detail"]


async def test_refresh_rotates_tokens(client: AsyncClient) -> None:
    """refresh 令牌换发新令牌对；access 令牌用于 refresh 应被拒绝。"""
    await _register(client, "frank")
    tokens = await _login(client, "frank")

    # access 令牌不能当 refresh 用（类型校验）
    resp = await client.post("/api/auth/refresh", json={"refresh_token": tokens["access_token"]})
    assert resp.status_code == 401

    # 合法 refresh 换发新对，且新 access 可用
    resp = await client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status_code == 200
    new_pair = resp.json()
    me = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {new_pair['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["username"] == "frank"


async def test_garbage_refresh_token_rejected(client: AsyncClient) -> None:
    """伪造 refresh 令牌返回 401。"""
    resp = await client.post("/api/auth/refresh", json={"refresh_token": "not-a-jwt"})
    assert resp.status_code == 401
