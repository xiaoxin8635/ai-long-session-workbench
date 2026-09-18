"""M-08 Chat 端点测试（Fake LLM 注入，不依赖外部模型服务）。"""

import json
from typing import Any

import pytest
from httpx import AsyncClient

from app.llm.client import LLMError, LLMNotConfigured, LLMUsage

_PASSWORD = "passw0rd123"


class FakeLLM:
    """可编程的 LLM 假实现：记录收到的上下文，返回固定/预设回答。"""

    def __init__(self, reply: str = "这是 EchoDesk 的测试回答。", fail: bool = False) -> None:
        """fail=True 时流式第二段抛 LLMError（降级路径）。"""
        self.reply = reply
        self.fail = fail
        self.received: list[dict[str, str]] = []

    async def stream_chat(self, messages: list[dict[str, str]]) -> Any:
        """流式：按 4 字符分片产出；fail 时第二片抛错。"""
        self.received = messages
        parts = [self.reply[i : i + 4] for i in range(0, len(self.reply), 4)]
        for i, part in enumerate(parts):
            if self.fail and i == 1:
                raise LLMError("上游中断（测试注入）")
            yield part

    async def complete(self, messages: list[dict[str, str]]) -> tuple[str, LLMUsage]:
        """非流式：返回完整回答。"""
        self.received = messages
        return self.reply, LLMUsage(prompt_tokens=10, completion_tokens=8)


@pytest.fixture
def fake_llm() -> FakeLLM:
    """默认可用的 Fake LLM。"""
    return FakeLLM()


@pytest.fixture
def chat_client(
    client: AsyncClient, fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """将路由内 get_llm_client 替换为 FakeLLM 的测试客户端。"""
    monkeypatch.setattr("app.api.routes.chat.get_llm_client", lambda: fake_llm)
    return client


async def _auth_setup(client: AsyncClient, username: str) -> tuple[dict, dict]:
    """注册登录并取个人 workspace，返回 (headers, ws)。"""
    await client.post(
        "/api/auth/register", json={"username": username, "password": _PASSWORD}
    )
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": _PASSWORD}
    )
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    ws = (await client.get("/api/workspaces", headers=headers)).json()[0]
    return headers, ws


def _body(ws_id: str, content: str, session_id: str | None = None, stream: bool = True) -> dict:
    """构造 chat 请求体。"""
    metadata: dict[str, Any] = {"workspace_id": ws_id}
    if session_id:
        metadata["session_id"] = session_id
    return {
        "messages": [{"role": "user", "content": content}],
        "stream": stream,
        "metadata": metadata,
    }


async def test_stream_chat_persists_roundtrip(chat_client: AsyncClient, fake_llm: FakeLLM) -> None:
    """流式：SSE 分片 + [DONE]；用户/回答双消息落库；会话自动创建。"""
    headers, ws = await _auth_setup(chat_client, "chat_stream")
    resp = await chat_client.post(
        "/v1/chat/completions", headers=headers, json=_body(ws["id"], "你好")
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    lines = [ln for ln in resp.text.splitlines() if ln.startswith("data: ")]
    data_lines = [ln[len("data: ") :] for ln in lines]
    assert data_lines[-1] == "[DONE]"
    payloads = [json.loads(ln) for ln in data_lines[:-1]]
    deltas = [p["choices"][0]["delta"].get("content", "") for p in payloads]
    assert "".join(deltas) == fake_llm.reply
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"

    # 会话自动创建（标题取首条用户消息）且两条消息落库
    sessions = (
        await chat_client.get(f"/api/sessions?workspace_id={ws['id']}", headers=headers)
    ).json()
    assert sessions["total"] == 1
    assert sessions["items"][0]["title"] == "你好"
    sid = sessions["items"][0]["id"]
    messages = (
        await chat_client.get(
            f"/api/sessions/{sid}/messages?workspace_id={ws['id']}", headers=headers
        )
    ).json()
    assert messages["total"] == 2
    assert [m["role"] for m in messages["items"]] == ["user", "assistant"]
    assert messages["items"][1]["content"] == fake_llm.reply


async def test_non_stream_chat_returns_openai_json(chat_client: AsyncClient) -> None:
    """非流式：标准 chat.completion 结构与 usage。"""
    headers, ws = await _auth_setup(chat_client, "chat_full")
    resp = await chat_client.post(
        "/v1/chat/completions",
        headers=headers,
        json=_body(ws["id"], "介绍一下自己", stream=False),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"]["completion_tokens"] > 0


async def test_multi_turn_working_memory_context(
    chat_client: AsyncClient, fake_llm: FakeLLM
) -> None:
    """多轮：第二轮 LLM 收到的上下文含首轮 user+assistant（Working Memory 生效）。"""
    headers, ws = await _auth_setup(chat_client, "chat_multi")
    first = await chat_client.post(
        "/v1/chat/completions", headers=headers, json=_body(ws["id"], "第一轮问题")
    )
    assert first.status_code == 200
    sessions = (
        await chat_client.get(f"/api/sessions?workspace_id={ws['id']}", headers=headers)
    ).json()
    sid = sessions["items"][0]["id"]

    second = await chat_client.post(
        "/v1/chat/completions",
        headers=headers,
        json=_body(ws["id"], "第二轮问题", session_id=sid),
    )
    assert second.status_code == 200
    context = [(m["role"], m["content"]) for m in fake_llm.received]
    assert ("user", "第一轮问题") in context
    assert ("assistant", fake_llm.reply) in context
    assert context[-1] == ("user", "第二轮问题")


async def test_chat_auth_and_isolation(chat_client: AsyncClient) -> None:
    """鉴权与隔离：无令牌 401 / 他人 workspace 403 / 非法 ws_id 422。"""
    headers, ws = await _auth_setup(chat_client, "chat_own")
    other_headers, _other_ws = await _auth_setup(chat_client, "chat_other")

    no_token = await chat_client.post("/v1/chat/completions", json=_body(ws["id"], "x"))
    assert no_token.status_code == 401

    cross = await chat_client.post(
        "/v1/chat/completions", headers=other_headers, json=_body(ws["id"], "x")
    )
    assert cross.status_code == 403

    bad_ws = await chat_client.post(
        "/v1/chat/completions", headers=headers, json=_body("not-a-uuid", "x")
    )
    assert bad_ws.status_code == 422


async def test_llm_not_configured_503(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM 未配置：503 problem+json（不 500）。"""
    def _raise() -> Any:
        raise LLMNotConfigured("LLM 未配置")

    monkeypatch.setattr("app.api.routes.chat.get_llm_client", _raise)
    headers, ws = await _auth_setup(client, "chat_unconf")
    resp = await client.post(
        "/v1/chat/completions", headers=headers, json=_body(ws["id"], "x")
    )
    assert resp.status_code == 503
    assert resp.json()["status"] == 503


async def test_llm_stream_error_degrades_gracefully(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """流中上游错误：SSE error 事件 + [DONE]，不裸 500。"""
    failing = FakeLLM(fail=True)
    monkeypatch.setattr("app.api.routes.chat.get_llm_client", lambda: failing)
    headers, ws = await _auth_setup(client, "chat_fail")
    resp = await client.post(
        "/v1/chat/completions", headers=headers, json=_body(ws["id"], "x")
    )
    assert resp.status_code == 200
    assert '"error"' in resp.text
    assert "[DONE]" in resp.text
