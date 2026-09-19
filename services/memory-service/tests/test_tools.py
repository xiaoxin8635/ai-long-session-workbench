"""M-09 工具子系统测试（docs/06 §11 DoD）。

覆盖：
  - 清单端点（名称/schema/风险分级）
  - todo 工具执行 + 审计落库（DoD：加待办且审计可查）+ 记忆同步
  - doc.read 读知识库文档（read_only）
  - 参数校验 422 / 未注册 404 / 工具异常降级为 failed 文本（DoD：不炸会话）
  - external 确认流：approve 执行 / deny 拒绝 / 超时拒绝 / 重复确认 409
    （web.fetch 承载；单测用注册的假 external 工具，真实出网在实机验证）
"""

import asyncio
import json
import uuid

import pytest
from httpx import AsyncClient
from pydantic import BaseModel

from app.core.config import get_settings
from app.models.enums import TaskStatus, ToolCallStatus, ToolRiskLevel
from app.tools.registry import ToolDefinition, default_registry

_PASSWORD = "passw0rd123"


# ---- 测试专用 external 工具（真实确认流，不出网）----


class _EchoArgs(BaseModel):
    """test.echo 参数。"""

    text: str


async def _echo(db, *, ws_id, user, session_id, args: _EchoArgs) -> dict:
    """原样返回参数（确认流执行体的最小替身）。"""
    return {"echo": args.text}


def _register_echo() -> str:
    """向默认注册表登记 external 假工具，返回工具名。"""
    name = f"test.echo.{uuid.uuid4().hex[:6]}"
    default_registry.register(
        ToolDefinition(
            name=name,
            description="测试专用 external 工具（确认流）",
            risk=ToolRiskLevel.EXTERNAL,
            args_model=_EchoArgs,
            handler=_echo,
        )
    )
    return name


async def _auth_setup(client: AsyncClient, username: str) -> tuple[dict, str]:
    """注册登录并取个人 workspace，返回 (headers, ws_id)。"""
    await client.post("/api/auth/register", json={"username": username, "password": _PASSWORD})
    resp = await client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    ws = (await client.get("/api/workspaces", headers=headers)).json()[0]
    return headers, ws["id"]


async def _wait_call_status(
    client: AsyncClient,
    headers: dict,
    ws_id: str,
    call_id: str,
    terminal: set[str],
    timeout: float = 8.0,
) -> dict:
    """轮询审计端点直至调用进入终态（后台确认任务的可见性通道）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        calls = (
            await client.get("/api/tools/calls", headers=headers, params={"workspace_id": ws_id})
        ).json()
        row = next((c for c in calls if c["id"] == call_id), None)
        if row and row["status"] in terminal:
            return row
        await asyncio.sleep(0.1)
    pytest.fail(f"等待调用终态超时：{call_id}")


def _fast_confirm(monkeypatch: pytest.MonkeyPatch, timeout: float = 1.5) -> None:
    """压缩确认等待窗口与轮询间隔（测试提速，不影响生产默认值）。"""
    monkeypatch.setattr(get_settings(), "tool_confirm_timeout_seconds", timeout)
    monkeypatch.setattr("app.tools.router._POLL_INTERVAL_SECONDS", 0.05)


# ---- 清单与执行 ----


async def test_tool_listing(client: AsyncClient) -> None:
    """清单含三个内置工具且风险分级正确、schema 可序列化。"""
    headers, ws_id = await _auth_setup(client, "tool_list")
    resp = await client.get("/api/tools", headers=headers)
    assert resp.status_code == 200
    tools = {t["name"]: t for t in resp.json()}
    assert tools["todo.create"]["risk"] == ToolRiskLevel.WRITE.value
    assert tools["doc.read"]["risk"] == ToolRiskLevel.READ_ONLY.value
    assert tools["web.fetch"]["risk"] == ToolRiskLevel.EXTERNAL.value
    assert "title" in tools["todo.create"]["args_schema"]["properties"]


async def test_todo_create_executes_and_audits(client: AsyncClient) -> None:
    """DoD：todo.create 写入 tasks 表且 tool_call_logs 审计可查。"""
    headers, ws_id = await _auth_setup(client, "tool_todo")
    sid = str(uuid.uuid4())
    resp = await client.post(
        "/api/tools/execute",
        headers=headers,
        params={"workspace_id": ws_id},
        json={"tool": "todo.create", "args": {"title": "字节面试准备"}, "session_id": sid},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == ToolCallStatus.SUCCESS.value
    assert body["result"]["task"]["title"] == "字节面试准备"

    tasks = (await client.get("/api/tasks", headers=headers, params={"workspace_id": ws_id})).json()
    assert [t["title"] for t in tasks] == ["字节面试准备"]
    assert sid in tasks[0]["related_session_ids"]

    calls = (
        await client.get("/api/tools/calls", headers=headers, params={"workspace_id": ws_id})
    ).json()
    assert calls and calls[0]["tool_name"] == "todo.create"
    assert calls[0]["status"] == ToolCallStatus.SUCCESS.value
    assert calls[0]["session_id"] == sid
    assert "字节面试准备" in calls[0]["result_digest"]


async def test_todo_update_via_tool_syncs_memory(client: AsyncClient) -> None:
    """todo.complete 完结任务并同步 job.*.progress 记忆（工具与 REST 同链路）。"""
    headers, ws_id = await _auth_setup(client, "tool_done")
    created = (
        await client.post(
            "/api/tools/execute",
            headers=headers,
            params={"workspace_id": ws_id},
            json={"tool": "todo.create", "args": {"title": "体检报告整理"}},
        )
    ).json()
    task_id = created["result"]["task"]["id"]

    done = (
        await client.post(
            "/api/tools/execute",
            headers=headers,
            params={"workspace_id": ws_id},
            json={"tool": "todo.complete", "args": {"task_id": task_id}},
        )
    ).json()
    assert done["result"]["task"]["status"] == TaskStatus.DONE.value

    memories = (
        await client.get("/api/memories", headers=headers, params={"workspace_id": ws_id})
    ).json()["items"]  # Page 结构：{"items": [...], "total": n}
    progress = [m for m in memories if m["key"].startswith("job:")]
    assert progress and "open → done" in progress[0]["content"]


async def test_doc_read_tool(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """doc.read 读知识库文档最新版（read_only；找不到显式 found=False）。"""
    monkeypatch.setattr("app.services.knowledge_service.spawn_ingest", lambda file_id: None)
    headers, ws_id = await _auth_setup(client, "tool_doc")
    await client.post(
        "/api/knowledge/files",
        headers=headers,
        params={"workspace_id": ws_id},
        files={
            "file": (
                "manual.md",
                "# 手册\n\nEchoDesk 部署采用 Docker Compose。".encode(),
                "text/markdown",
            )
        },
    )
    hit = (
        await client.post(
            "/api/tools/execute",
            headers=headers,
            params={"workspace_id": ws_id},
            json={"tool": "doc.read", "args": {"filename": "manual.md"}},
        )
    ).json()
    assert hit["status"] == ToolCallStatus.SUCCESS.value
    assert hit["result"]["found"] is True
    assert "Docker Compose" in hit["result"]["text"]

    miss = (
        await client.post(
            "/api/tools/execute",
            headers=headers,
            params={"workspace_id": ws_id},
            json={"tool": "doc.read", "args": {"filename": "不存在.md"}},
        )
    ).json()
    assert miss["result"]["found"] is False


async def test_tool_validation_and_not_found(client: AsyncClient) -> None:
    """未注册工具 404；参数不合法 422（RFC 7807）。"""
    headers, ws_id = await _auth_setup(client, "tool_422")
    unknown = await client.post(
        "/api/tools/execute",
        headers=headers,
        params={"workspace_id": ws_id},
        json={"tool": "no.such.tool", "args": {}},
    )
    assert unknown.status_code == 404
    bad_args = await client.post(
        "/api/tools/execute",
        headers=headers,
        params={"workspace_id": ws_id},
        json={"tool": "todo.create", "args": {"title": ""}},
    )
    assert bad_args.status_code == 422


async def test_tool_failure_degrades_to_text(client: AsyncClient) -> None:
    """DoD：工具异常不炸调用方——返回 status=failed + 错误说明并落审计。"""
    headers, ws_id = await _auth_setup(client, "tool_fail")
    resp = await client.post(
        "/api/tools/execute",
        headers=headers,
        params={"workspace_id": ws_id},
        json={"tool": "todo.update", "args": {"task_id": str(uuid.uuid4()), "status": "doing"}},
    )
    assert resp.status_code == 200  # 工具级失败是合法结果，非服务错误
    body = resp.json()
    assert body["status"] == ToolCallStatus.FAILED.value
    assert "任务" in body["error"]
    calls = (
        await client.get("/api/tools/calls", headers=headers, params={"workspace_id": ws_id})
    ).json()
    assert calls[0]["status"] == ToolCallStatus.FAILED.value


# ---- external 确认流 ----


async def test_external_approve_executes(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """external 工具经确认后执行成功（approver 落审计）。"""
    _fast_confirm(monkeypatch)
    headers, ws_id = await _auth_setup(client, "tool_ok")
    name = _register_echo()

    exec_resp = await client.post(
        "/api/tools/execute",
        headers=headers,
        params={"workspace_id": ws_id},
        json={"tool": name, "args": {"text": "确认后执行"}},
    )
    assert exec_resp.status_code == 202
    pending = exec_resp.json()
    assert pending["requires_confirmation"] is True
    assert pending["status"] == ToolCallStatus.PENDING.value

    confirmed = await client.post(
        f"/api/tools/calls/{pending['call_id']}/confirm",
        headers=headers,
        params={"workspace_id": ws_id},
        json={"approve": True},
    )
    assert confirmed.status_code == 200
    row = await _wait_call_status(
        client, headers, ws_id, pending["call_id"], {ToolCallStatus.SUCCESS.value}
    )
    assert json.loads(row["result_digest"])["echo"] == "确认后执行"
    assert row["approver_id"]


async def test_external_deny_rejected(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """用户拒绝 → DENIED 终态，不执行。"""
    _fast_confirm(monkeypatch)
    headers, ws_id = await _auth_setup(client, "tool_deny")
    name = _register_echo()
    pending = (
        await client.post(
            "/api/tools/execute",
            headers=headers,
            params={"workspace_id": ws_id},
            json={"tool": name, "args": {"text": "x"}},
        )
    ).json()
    await client.post(
        f"/api/tools/calls/{pending['call_id']}/confirm",
        headers=headers,
        params={"workspace_id": ws_id},
        json={"approve": False},
    )
    row = await _wait_call_status(
        client, headers, ws_id, pending["call_id"], {ToolCallStatus.DENIED.value}
    )
    assert "拒绝" in row["result_digest"]


async def test_external_timeout_rejected(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DoD：external 未确认 → 超时拒绝，审计落 timeout。"""
    _fast_confirm(monkeypatch, timeout=1.0)
    headers, ws_id = await _auth_setup(client, "tool_timeout")
    name = _register_echo()
    pending = (
        await client.post(
            "/api/tools/execute",
            headers=headers,
            params={"workspace_id": ws_id},
            json={"tool": name, "args": {"text": "x"}},
        )
    ).json()
    row = await _wait_call_status(
        client, headers, ws_id, pending["call_id"], {ToolCallStatus.TIMEOUT.value}
    )
    assert "超时" in row["result_digest"]


async def test_confirm_non_pending_409(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重复确认（已终态）409；他人 workspace 的调用记录不可见（404 防枚举）。"""
    _fast_confirm(monkeypatch)
    headers, ws_id = await _auth_setup(client, "tool_409")
    name = _register_echo()
    pending = (
        await client.post(
            "/api/tools/execute",
            headers=headers,
            params={"workspace_id": ws_id},
            json={"tool": name, "args": {"text": "x"}},
        )
    ).json()
    call_id = pending["call_id"]
    await client.post(
        f"/api/tools/calls/{call_id}/confirm",
        headers=headers,
        params={"workspace_id": ws_id},
        json={"approve": False},
    )
    await _wait_call_status(client, headers, ws_id, call_id, {ToolCallStatus.DENIED.value})
    again = await client.post(
        f"/api/tools/calls/{call_id}/confirm",
        headers=headers,
        params={"workspace_id": ws_id},
        json={"approve": True},
    )
    assert again.status_code == 409

    # 跨 workspace：另一用户确认他人调用 → 记录不可见（404）
    other_headers, _ = await _auth_setup(client, "tool_409_other")
    foreign = await client.post(
        f"/api/tools/calls/{call_id}/confirm",
        headers=other_headers,
        params={"workspace_id": "00000000-0000-0000-0000-000000000000"},
        json={"approve": True},
    )
    assert foreign.status_code in (403, 404)
