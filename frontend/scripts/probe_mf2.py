# -*- coding: utf-8 -*-
"""M-F2 实机验证探针：经 vite dev proxy(3200) 走完整链路。

流程：注册/登录探针账号 → 拉 workspace → POST /v1/chat/completions 流式
（校验 session_id 捕获 / delta 增量 / finish 收敛）→ 回读会话列表与消息历史
（校验自动建会话与历史回放）。全部请求经 http://127.0.0.1:3200，验证代理转发。
"""
import json
import sys
import time
import uuid

import httpx

BASE = "http://localhost:3200"
USERNAME = "mf2_probe_" + uuid.uuid4().hex[:8]
PASSWORD = "Probe#2026@f2"

sys.stdout.reconfigure(encoding="utf-8")


def main() -> None:
    """执行探针主流程并打印各步骤判定结果。"""
    # trust_env=False：忽略系统代理（本机 Clash 类代理会把 127.0.0.1 请求劫持成 502）
    client = httpx.Client(
        base_url=BASE, trust_env=False, timeout=httpx.Timeout(30.0, read=180.0)
    )

    # 1) 注册 + 登录
    resp = client.post(
        "/api/auth/register",
        json={"username": USERNAME, "password": PASSWORD, "display_name": "MF2探针"},
    )
    print("[register]", resp.status_code)
    resp.raise_for_status()

    resp = client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
    print("[login]", resp.status_code)
    resp.raise_for_status()
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 2) workspace
    resp = client.get("/api/workspaces", headers=headers)
    print("[workspaces]", resp.status_code)
    resp.raise_for_status()
    ws_id = resp.json()[0]["id"]
    print("[workspace_id]", ws_id)

    # 3) 流式对话（草稿态：不带 session_id，验证自动建会话）
    body = {
        "messages": [{"role": "user", "content": "请用一句话介绍你自己，并记住我喜欢简洁的回答。"}],
        "stream": True,
        "metadata": {"workspace_id": ws_id, "session_id": None, "enable_tools": True},
    }
    t0 = time.time()
    with client.stream("POST", "/v1/chat/completions", json=body, headers=headers) as resp:
        print("[chat status]", resp.status_code)
        resp.raise_for_status()
        got_session_id = None
        deltas = []
        finish_reason = None
        has_done = False
        first_token_ms = None
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[len("data: "):]
            if payload == "[DONE]":
                has_done = True
                break
            chunk = json.loads(payload)
            meta = chunk.get("metadata") or {}
            if meta.get("session_id"):
                got_session_id = meta["session_id"]
            choices = chunk.get("choices") or []
            if choices:
                delta = (choices[0].get("delta") or {}).get("content")
                if delta:
                    if first_token_ms is None:
                        first_token_ms = int((time.time() - t0) * 1000)
                    deltas.append(delta)
                if choices[0].get("finish_reason"):
                    finish_reason = choices[0]["finish_reason"]
                    print("[citations]", meta.get("citations"))
                    print("[pending_confirmation]", meta.get("pending_confirmation"))

    answer = "".join(deltas)
    print("[ttfb_ms]", first_token_ms)
    print("[deltas_chunks]", len(deltas))
    print("[finish_reason]", finish_reason)
    print("[has_done]", has_done)
    print("[session_id]", got_session_id)
    print("[answer]", answer[:200])

    assert got_session_id, "未捕获 session_id"
    assert len(answer) > 0, "未收到任何正文增量"
    assert finish_reason == "stop", f"finish_reason 异常: {finish_reason}"
    assert has_done, "未收到 [DONE]"

    # 4) 会话列表 + 历史回放
    resp = client.get(f"/api/sessions?workspace_id={ws_id}&limit=50", headers=headers)
    print("[sessions]", resp.status_code, "total=", resp.json()["total"])
    resp.raise_for_status()
    sessions = resp.json()["items"]
    assert any(s["id"] == got_session_id for s in sessions), "自动创建的会话未出现在列表"

    resp = client.get(
        f"/api/sessions/{got_session_id}/messages?workspace_id={ws_id}&limit=100",
        headers=headers,
    )
    print("[messages]", resp.status_code)
    resp.raise_for_status()
    roles = [m["role"] for m in resp.json()["items"]]
    print("[message_roles]", roles)
    assert "user" in roles and "assistant" in roles, "历史回放缺少 user/assistant 消息"

    print("=== M-F2 探针全部通过 ===")


if __name__ == "__main__":
    main()
