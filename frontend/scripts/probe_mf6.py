# -*- coding: utf-8 -*-
"""M-F6 实机验证探针：web 容器（Nginx）接管 3000 后的部署全链路。

验证点：
  1. 静态托管：GET / 返回 index.html（含 EchoDesk 标题），静态资源可加载；
  2. REST 反代：/api（注册 → 登录 → workspace 列表）经 Nginx 同源转发；
  3. SSE 反代：/v1 流式对话——chunked 传输（无 content-length）、
     session_id 捕获、增量 delta、finish stop、[DONE]、历史回放一致。

直连 http://127.0.0.1:3000（web 容器），trust_env=False 绕过系统代理。
必须用 127.0.0.1 而非 localhost：本机 wslrelay 劫持 [::1] 端口转发会
直接断连（与 vite proxy target 同款坑，见 docs/04 §11）。
"""
import re
import sys
import time
import uuid

import httpx

BASE = "http://127.0.0.1:3000"
USERNAME = "mf6_probe_" + uuid.uuid4().hex[:8]
PASSWORD = "Probe#2026@f6"

sys.stdout.reconfigure(encoding="utf-8")


def main() -> None:
    """执行探针主流程并打印各步骤判定结果。"""
    client = httpx.Client(
        base_url=BASE, trust_env=False, timeout=httpx.Timeout(30.0, read=180.0)
    )

    # 1) 静态托管：SPA 入口 + 静态资源抽查
    resp = client.get("/")
    print("[index]", resp.status_code, resp.headers.get("content-type"))
    resp.raise_for_status()
    assert "EchoDesk" in resp.text, "index.html 未包含 EchoDesk 标识"
    assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', resp.text)
    assert len(assets) >= 1, "index.html 未引用任何 /assets/ 静态资源"
    resp = client.get(assets[0])
    print("[asset]", assets[0], resp.status_code)
    resp.raise_for_status()

    # 2) REST 反代：注册 → 登录 → workspace
    resp = client.post(
        "/api/auth/register",
        json={"username": USERNAME, "password": PASSWORD, "display_name": "MF6探针"},
    )
    print("[register]", resp.status_code)
    resp.raise_for_status()
    resp = client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
    resp.raise_for_status()
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.get("/api/workspaces", headers=headers)
    resp.raise_for_status()
    ws_id = resp.json()[0]["id"]
    print("[rest via /api] workspace", ws_id[:8], "…")

    # 3) SSE 反代：流式对话（断言 chunked 传输 + 事件契约完整）
    events: list[tuple[str, float]] = []  # (事件类型, 相对时刻)
    session_id: str | None = None
    deltas: list[str] = []
    finished = False
    done_seen = False
    start = time.monotonic()

    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={**headers, "Content-Type": "application/json"},
        json={
            "messages": [{"role": "user", "content": "用一句话介绍 EchoDesk。"}],
            "stream": True,
            "metadata": {"workspace_id": ws_id, "session_id": None, "enable_tools": False},
        },
    ) as stream:
        # chunked 且无 content-length：SSE 未被 Nginx 攒成一个整体响应
        assert stream.headers.get("content-length") is None, "SSE 响应带 content-length（被缓冲）"
        print("[sse headers] transfer-encoding =", stream.headers.get("transfer-encoding"))

        for line in stream.iter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[len("data: "):]
            if payload == "[DONE]":
                done_seen = True
                events.append(("done", time.monotonic() - start))
                break
            import json as _json
            chunk = _json.loads(payload)
            if "error" in chunk:
                raise AssertionError(f"流内错误事件：{chunk['error']}")
            meta = chunk.get("metadata") or {}
            if isinstance(meta.get("session_id"), str) and session_id is None:
                session_id = meta["session_id"]
                events.append(("session_id", time.monotonic() - start))
            choice = (chunk.get("choices") or [{}])[0]
            content = (choice.get("delta") or {}).get("content")
            if isinstance(content, str) and content:
                deltas.append(content)
                events.append(("delta", time.monotonic() - start))
            if choice.get("finish_reason") == "stop":
                finished = True
                events.append(("finish", time.monotonic() - start))

    assert session_id is not None, "未捕获 session_id"
    assert len(deltas) >= 1, "未收到任何正文增量"
    assert finished and done_seen, f"流未正常收敛（finish={finished}, done={done_seen}）"
    timeline = " ".join(f"{name}@{t:.2f}s" for name, t in events[:6])
    print(f"[sse] deltas={len(deltas)} {timeline} …")

    # 4) 历史回放：消息已落库且含用户问题与助手回答
    resp = client.get(
        f"/api/sessions/{session_id}/messages",
        params={"workspace_id": ws_id},
        headers=headers,
    )
    resp.raise_for_status()
    messages = resp.json()["items"]
    assert any(m["role"] == "user" and "EchoDesk" in m["content"] for m in messages)
    assert any(m["role"] == "assistant" for m in messages), "助手回复未落库"
    print("[replay]", len(messages), "条消息，user/assistant 均落库")

    print("=== M-F6 探针全部通过（静态托管/REST 反代/SSE 流式/回放一致）===")


if __name__ == "__main__":
    main()
