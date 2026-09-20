# -*- coding: utf-8 -*-
"""M-F4 实机验证探针：记忆工作台后端契约全链路。

流程：注册/登录 → 非流式对话植入事实（触发抽取队列）→ 轮询列表等落库 →
检索测试（同对话链路检索器）→ 用户编辑（version+1）→ 上下文装配预览 →
软删除复核。冲突裁决（resolve）依赖真实冲突难确定性构造，由单测覆盖。
经 http://localhost:3200 vite proxy，trust_env=False 绕过系统代理。
"""
import json
import sys
import time
import uuid

import httpx

BASE = "http://localhost:3200"
USERNAME = "mf4_probe_" + uuid.uuid4().hex[:8]
PASSWORD = "Probe#2026@f4"

sys.stdout.reconfigure(encoding="utf-8")


def main() -> None:
    """执行探针主流程并打印各步骤判定结果。"""
    client = httpx.Client(
        base_url=BASE, trust_env=False, timeout=httpx.Timeout(30.0, read=180.0)
    )

    # 1) 注册 + 登录 + workspace
    resp = client.post(
        "/api/auth/register",
        json={"username": USERNAME, "password": PASSWORD, "display_name": "MF4探针"},
    )
    print("[register]", resp.status_code)
    resp.raise_for_status()
    resp = client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
    resp.raise_for_status()
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    resp = client.get("/api/workspaces", headers=headers)
    resp.raise_for_status()
    ws_id = resp.json()[0]["id"]

    # 2) 非流式对话植入事实（抽取走异步队列）
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {"role": "user", "content": "请记住：我的研发团队代号叫猎鹰，办公地点在杭州。"}
            ],
            "stream": False,
            "metadata": {"workspace_id": ws_id, "session_id": None, "enable_tools": False},
        },
        headers=headers,
    )
    print("[chat]", resp.status_code)
    resp.raise_for_status()
    session_id = resp.json()["metadata"]["session_id"]
    print("[session_id]", session_id)

    # 3) 轮询列表等抽取落库（队列消化约 2~5 条/分钟，上限 180s）
    target = None
    deadline = time.time() + 180
    while time.time() < deadline and target is None:
        time.sleep(6)
        resp = client.get(
            f"/api/memories?workspace_id={ws_id}&limit=50", headers=headers
        )
        resp.raise_for_status()
        for item in resp.json()["items"]:
            if "猎鹰" in item["content"] or "猎鹰" in item["key"]:
                target = item
                break
    assert target is not None, "180s 内未见团队代号记忆落库"
    print("[memory]", target["memory_type"], target["key"], "v", target["version"])
    print("[status]", target["status"])

    # 4) 检索测试（与对话链路同检索器）
    resp = client.post(
        "/api/memories/search",
        json={"query": "我的研发团队代号是什么", "top_k": 5},
        params={"workspace_id": ws_id},
        headers=headers,
    )
    print("[search]", resp.status_code)
    resp.raise_for_status()
    hits = resp.json()
    print("[hits]", [(h["key"], round(h["score"], 3)) for h in hits])
    assert len(hits) >= 1, "检索无命中"
    assert any("猎鹰" in h["content"] for h in hits), "命中未包含团队代号内容"

    # 5) 用户编辑（version+1）
    resp = client.patch(
        f"/api/memories/{target['id']}",
        json={"content": target["content"] + "（已由用户核实）"},
        params={"workspace_id": ws_id},
        headers=headers,
    )
    print("[patch]", resp.status_code)
    resp.raise_for_status()
    edited = resp.json()
    assert edited["version"] == target["version"] + 1, f"版本号未递增: {edited['version']}"
    assert "已由用户核实" in edited["content"]

    # 6) 上下文装配预览
    resp = client.post(
        "/api/context/preview",
        json={
            "workspace_id": ws_id,
            "session_id": session_id,
            "query": "我的研发团队代号是什么？",
        },
        headers=headers,
    )
    print("[preview]", resp.status_code)
    resp.raise_for_status()
    preview = resp.json()
    sections = {s["key"]: s for s in preview["sections"]}
    print(
        "[sections]",
        {k: (v["included"], v["tokens"]) for k, v in sections.items()},
    )
    assert preview["total_tokens"] > 0, "装配 token 总数为 0"
    assert len(preview["messages"]) >= 1, "装配产物为空"

    # 7) 软删除复核（列表不再可见）
    resp = client.delete(
        f"/api/memories/{target['id']}",
        params={"workspace_id": ws_id},
        headers=headers,
    )
    print("[delete]", resp.status_code)
    resp.raise_for_status()
    resp = client.get(f"/api/memories?workspace_id={ws_id}&limit=50", headers=headers)
    resp.raise_for_status()
    remaining_ids = [m["id"] for m in resp.json()["items"]]
    assert target["id"] not in remaining_ids, "软删除后仍出现在列表"

    print("=== M-F4 探针全部通过（抽取落库/检索/编辑 v+1/预览/软删除）===")


if __name__ == "__main__":
    main()
