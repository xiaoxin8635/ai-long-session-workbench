# -*- coding: utf-8 -*-
"""M-F3 实机验证探针：external 工具确认流全链路（确认 + 拒绝）。

流程：注册/登录 → GET /api/tools 清单 → 流式对话诱导 web.fetch（external）
→ 断言 SSE tool_call 事件 → /v1/chat/resume approve=true 续传 → 审计核对
success；再来一轮 resume approve=false → 审计核对 denied。
经 http://localhost:3200 vite proxy，trust_env=False 绕过系统代理。
"""
import json
import sys
import uuid

import httpx

BASE = "http://localhost:3200"
USERNAME = "mf3_probe_" + uuid.uuid4().hex[:8]
PASSWORD = "Probe#2026@f3"

sys.stdout.reconfigure(encoding="utf-8")


def sse_turn(client: httpx.Client, path: str, body: dict, headers: dict) -> dict:
    """消费一次聊天 SSE 流。

    Args:
        client: httpx 客户端。
        path: /v1/chat/completions 或 /v1/chat/resume。
        body: 请求体。
        headers: 认证头。

    Returns:
        {deltas, session_id, tool_call, finish, done} 汇总。
    """
    out = {"deltas": [], "session_id": None, "tool_call": None, "finish": None, "done": False}
    with client.stream("POST", path, json=body, headers=headers) as resp:
        print(f"[{path}]", resp.status_code)
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[len("data: "):]
            if payload == "[DONE]":
                out["done"] = True
                break
            chunk = json.loads(payload)
            if "error" in chunk:
                raise AssertionError(f"服务端 error 事件: {chunk['error']}")
            meta = chunk.get("metadata") or {}
            if meta.get("session_id"):
                out["session_id"] = meta["session_id"]
            if meta.get("tool_call"):
                out["tool_call"] = meta["tool_call"]
            choices = chunk.get("choices") or []
            if choices:
                delta = (choices[0].get("delta") or {}).get("content")
                if delta:
                    out["deltas"].append(delta)
                if choices[0].get("finish_reason") == "stop":
                    out["finish"] = meta
    return out


def main() -> None:
    """执行探针主流程并打印各步骤判定结果。"""
    client = httpx.Client(
        base_url=BASE, trust_env=False, timeout=httpx.Timeout(30.0, read=180.0)
    )

    # 1) 注册 + 登录 + workspace
    resp = client.post(
        "/api/auth/register",
        json={"username": USERNAME, "password": PASSWORD, "display_name": "MF3探针"},
    )
    print("[register]", resp.status_code)
    resp.raise_for_status()
    resp = client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
    resp.raise_for_status()
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    resp = client.get("/api/workspaces", headers=headers)
    resp.raise_for_status()
    ws_id = resp.json()[0]["id"]

    # 2) 工具清单
    resp = client.get("/api/tools", headers=headers)
    resp.raise_for_status()
    tools = {t["name"]: t["risk"] for t in resp.json()}
    print("[tools]", tools)
    assert tools.get("web.fetch") == "external", "web.fetch 应为 external 风险"

    # 3) 第一轮：诱导 web.fetch → 期望 tool_call 挂起
    turn = sse_turn(
        client,
        "/v1/chat/completions",
        {
            "messages": [{"role": "user", "content": "请调用 web.fetch 工具抓取 https://example.com"}],
            "stream": True,
            "metadata": {"workspace_id": ws_id, "session_id": None, "enable_tools": True},
        },
        headers,
    )
    session_id = turn["session_id"]
    call = turn["tool_call"]
    print("[session_id]", session_id)
    print("[tool_call]", call)
    assert session_id and call, "未收到 session_id / tool_call 事件"
    assert call["tool"] == "web.fetch", f"确认请求工具异常: {call}"
    call_id = call["call_id"]

    # finish 片应带 pending_confirmation
    print("[pending_confirmation]", (turn["finish"] or {}).get("pending_confirmation"))

    # 4) approve=true 续传
    resume = sse_turn(
        client,
        "/v1/chat/resume",
        {
            "workspace_id": ws_id,
            "session_id": session_id,
            "call_id": call_id,
            "approve": True,
        },
        headers,
    )
    answer = "".join(resume["deltas"])
    print("[approve answer]", answer[:150])
    assert resume["done"] and len(answer) > 0, "approve 续传无正文"

    # 5) 审计核对 approve 终态
    resp = client.get(f"/api/tools/calls?workspace_id={ws_id}&limit=20", headers=headers)
    resp.raise_for_status()
    logs = {c["id"]: c for c in resp.json()}
    approved = logs[call_id]
    print("[audit approve]", approved["status"], approved["risk_level"])
    assert approved["status"] == "success", f"approve 后审计状态异常: {approved['status']}"

    # 6) 第二轮：拒绝路径
    turn2 = sse_turn(
        client,
        "/v1/chat/completions",
        {
            "messages": [{"role": "user", "content": "请再调用 web.fetch 工具抓取 https://example.com"}],
            "stream": True,
            "metadata": {"workspace_id": ws_id, "session_id": None, "enable_tools": True},
        },
        headers,
    )
    call2 = turn2["tool_call"]
    assert call2, "第二轮未收到 tool_call"
    resume2 = sse_turn(
        client,
        "/v1/chat/resume",
        {
            "workspace_id": ws_id,
            "session_id": turn2["session_id"],
            "call_id": call2["call_id"],
            "approve": False,
        },
        headers,
    )
    answer2 = "".join(resume2["deltas"])
    print("[deny answer]", answer2[:150])
    assert resume2["done"], "deny 续传未收敛"

    # 7) 审计核对 deny 终态
    resp = client.get(f"/api/tools/calls?workspace_id={ws_id}&limit=20", headers=headers)
    resp.raise_for_status()
    logs2 = {c["id"]: c for c in resp.json()}
    denied = logs2[call2["call_id"]]
    print("[audit deny]", denied["status"])
    assert denied["status"] == "denied", f"deny 后审计状态异常: {denied['status']}"

    print("=== M-F3 探针全部通过（approve success + deny denied）===")


if __name__ == "__main__":
    main()
