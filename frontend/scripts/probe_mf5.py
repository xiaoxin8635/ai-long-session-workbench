# -*- coding: utf-8 -*-
"""M-F5 实机验证探针：知识库 / 任务 / 用量 / 设置（成员管理）契约全链路。

流程：注册探针账号 → 非流式对话一轮（供用量统计）→ 任务创建与状态流转
（open → doing → done + 过滤）→ 知识文件上传（multipart）→ 轮询 parsing →
embedded → 检索命中 → 删除 → 用量汇总（turns/token 非零）→ 成员管理
（注册第二用户并添加为 member）。经 http://localhost:3200 vite proxy，
trust_env=False 绕过系统代理。
"""
import io
import sys
import time
import uuid

import httpx

BASE = "http://localhost:3200"
USERNAME = "mf5_probe_" + uuid.uuid4().hex[:8]
PASSWORD = "Probe#2026@f5"
USER_B = "mf5b_" + uuid.uuid4().hex[:8]

sys.stdout.reconfigure(encoding="utf-8")

# 上传用知识文件内容（部署主题，便于检索断言）
DOC_NAME = "echodesk-deploy.md"
DOC_CONTENT = (
    "# EchoDesk 部署指南\n\n"
    "使用 Docker Compose 一键部署 memory-service 与前端工作台。\n"
    "知识检索与对话 RAG 区块同口径：向量 + BM25 混合召回后重排。\n"
)


def main() -> None:
    """执行探针主流程并打印各步骤判定结果。"""
    client = httpx.Client(
        base_url=BASE, trust_env=False, timeout=httpx.Timeout(30.0, read=180.0)
    )

    # 1) 注册 + 登录 + workspace
    resp = client.post(
        "/api/auth/register",
        json={"username": USERNAME, "password": PASSWORD, "display_name": "MF5探针"},
    )
    print("[register]", resp.status_code)
    resp.raise_for_status()
    resp = client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
    resp.raise_for_status()
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    resp = client.get("/api/workspaces", headers=headers)
    resp.raise_for_status()
    ws_id = resp.json()[0]["id"]

    # 2) 非流式对话一轮（产生 token_usages 行，供用量断言）
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "用一句话介绍你自己。"}],
            "stream": False,
            "metadata": {"workspace_id": ws_id, "session_id": None, "enable_tools": False},
        },
        headers=headers,
    )
    print("[chat]", resp.status_code)
    resp.raise_for_status()

    # 3) 任务：创建 → 流转 open→doing→done + 状态过滤
    resp = client.post(
        "/api/tasks",
        json={"title": "整理部署文档", "priority": 1},
        params={"workspace_id": ws_id},
        headers=headers,
    )
    print("[task create]", resp.status_code)
    resp.raise_for_status()
    task = resp.json()
    assert task["status"] == "open" and task["priority"] == 1

    resp = client.patch(
        f"/api/tasks/{task['id']}",
        json={"status": "doing"},
        params={"workspace_id": ws_id},
        headers=headers,
    )
    resp.raise_for_status()
    assert resp.json()["status"] == "doing"
    resp = client.get(
        "/api/tasks", params={"workspace_id": ws_id, "status": "doing"}, headers=headers
    )
    resp.raise_for_status()
    assert any(t["id"] == task["id"] for t in resp.json()), "doing 过滤未命中"
    resp = client.patch(
        f"/api/tasks/{task['id']}",
        json={"status": "done"},
        params={"workspace_id": ws_id},
        headers=headers,
    )
    resp.raise_for_status()
    assert resp.json()["status"] == "done"
    print("[task flow] open→doing→done 过滤均通过")

    # 4) 知识库：上传 → 轮询 embedded → 检索 → 删除
    resp = client.post(
        "/api/knowledge/files",
        params={"workspace_id": ws_id},
        headers=headers,
        files={"file": (DOC_NAME, io.BytesIO(DOC_CONTENT.encode("utf-8")), "text/markdown")},
    )
    print("[kb upload]", resp.status_code)
    resp.raise_for_status()
    kb_file = resp.json()
    assert kb_file["status"] in ("parsing", "embedded"), kb_file["status"]

    deadline = time.time() + 90
    status = kb_file["status"]
    while time.time() < deadline and status != "embedded":
        time.sleep(3)
        resp = client.get(
            "/api/knowledge/files", params={"workspace_id": ws_id}, headers=headers
        )
        resp.raise_for_status()
        rows = resp.json()
        status = next(f["status"] for f in rows if f["id"] == kb_file["id"])
    assert status == "embedded", f"90s 内未完成向量化（当前 {status}）"
    print("[kb embedded] 切片数", kb_file["chunk_count"])

    resp = client.post(
        "/api/knowledge/search",
        json={"query": "怎么部署 EchoDesk", "top_k": 5},
        params={"workspace_id": ws_id},
        headers=headers,
    )
    print("[kb search]", resp.status_code)
    resp.raise_for_status()
    hits = resp.json()["hits"]
    print("[kb hits]", [(h["filename"], h["chunk_index"], round(h["score"], 3)) for h in hits])
    assert len(hits) >= 1, "知识检索无命中"
    assert any("Docker Compose" in h["content"] for h in hits), "命中内容不含目标片段"

    resp = client.delete(
        f"/api/knowledge/files/{kb_file['id']}",
        params={"workspace_id": ws_id},
        headers=headers,
    )
    print("[kb delete]", resp.status_code)
    resp.raise_for_status()
    resp = client.get(
        "/api/knowledge/files", params={"workspace_id": ws_id}, headers=headers
    )
    resp.raise_for_status()
    rows = resp.json()
    assert all(f["id"] != kb_file["id"] for f in rows), "删除后文件仍在列表"

    # 5) 用量汇总（窗口 7 天，应有本轮对话数据）
    resp = client.get(
        "/api/usage/summary", params={"workspace_id": ws_id, "days": 7}, headers=headers
    )
    print("[usage]", resp.status_code)
    resp.raise_for_status()
    usage = resp.json()
    print(
        "[usage]",
        "turns =", usage["turns"],
        "total =", usage["totals"]["total_tokens"],
        "by_day =", len(usage["by_day"]),
    )
    assert usage["turns"] >= 1, "轮次数为 0"
    assert usage["totals"]["total_tokens"] > 0, "token 总量为 0"

    # 6) 成员管理：注册第二用户并添加为 member
    resp = client.post(
        "/api/auth/register",
        json={"username": USER_B, "password": PASSWORD, "display_name": "MF5成员B"},
    )
    resp.raise_for_status()
    resp = client.post(
        f"/api/workspaces/{ws_id}/members",
        json={"username": USER_B, "role": "member"},
        headers=headers,
    )
    print("[member add]", resp.status_code)
    resp.raise_for_status()
    assert resp.json()["role"] == "member"
    resp = client.get(f"/api/workspaces/{ws_id}/members", headers=headers)
    resp.raise_for_status()
    names = {m["username"]: m["role"] for m in resp.json()}
    assert names.get(USERNAME) == "owner" and names.get(USER_B) == "member", names
    print("[members]", names)

    print("=== M-F5 探针全部通过（任务流转/知识检索/用量汇总/成员管理）===")


if __name__ == "__main__":
    main()
