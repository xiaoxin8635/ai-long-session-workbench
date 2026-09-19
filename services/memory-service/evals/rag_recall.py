"""RAG 召回率冒烟评测（M-07 承诺：轻量 Recall@K，真实链路）。

流程：注册临时用户 → 上传样例文档 → 轮询至 embedded → 逐条查询
  /api/knowledge/search → 统计 Recall@K 与 MRR → 打印明细与汇总。

用法（服务已启动、embedding/infinity 容器在跑）：
  python evals/rag_recall.py [--base-url http://localhost:8100] [--top-k 5]

数据集：evals/data/rag_smoke.md（多主题段落）+ rag_queries.jsonl
（query 与期望命中的段落子串一一对应）。
完整评测（分层指标、多数据集）在 M-13 落地，本脚本为开发期快速自测。
"""

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

_DATA_DIR = Path(__file__).resolve().parent / "data"
_PASSWORD = "eval_passw0rd"

# 状态轮询参数（embedding 后台摄取通常 <30s）
_POLL_INTERVAL_SECONDS = 2.0
_POLL_TIMEOUT_SECONDS = 120.0


def _login(client: httpx.Client, base_url: str) -> tuple[dict, str]:
    """注册并登录评测用户，返回 (headers, workspace_id)。

    Args:
        client: HTTP 客户端。
        base_url: memory-service 根地址。

    Returns:
        (Authorization 头, 个人 workspace ID)。
    """
    username = f"rag_eval_{uuid.uuid4().hex[:8]}"
    client.post(f"{base_url}/api/auth/register", json={"username": username, "password": _PASSWORD})
    resp = client.post(
        f"{base_url}/api/auth/login", json={"username": username, "password": _PASSWORD}
    )
    resp.raise_for_status()
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    workspaces = client.get(f"{base_url}/api/workspaces", headers=headers)
    workspaces.raise_for_status()
    return headers, workspaces.json()[0]["id"]


def _wait_embedded(client: httpx.Client, base_url: str, headers: dict, ws_id: str) -> str:
    """轮询上传文件直至 embedded，返回 file_id。

    Raises:
        TimeoutError: 超过轮询上限仍未完成。
        RuntimeError: 文件进入 failed 状态。
    """
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        listing = client.get(
            f"{base_url}/api/knowledge/files", headers=headers, params={"workspace_id": ws_id}
        )
        listing.raise_for_status()
        files = listing.json()
        if files:
            status = files[0]["status"]
            if status == "embedded":
                return str(files[0]["id"])
            if status == "failed":
                raise RuntimeError("文件摄取失败（embedding 服务不可用？）")
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise TimeoutError("等待摄取超时")


def _load_queries() -> list[dict]:
    """加载评测数据集（query + 期望命中的正文子串）。"""
    lines = (_DATA_DIR / "rag_queries.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def run(base_url: str, top_k: int) -> int:
    """执行评测并打印报告。

    Args:
        base_url: 服务根地址。
        top_k: 检索条数（Recall@K 的 K）。

    Returns:
        进程退出码（0 = 全部命中；1 = 存在未命中，便于 CI 拦截）。
    """
    queries = _load_queries()
    with httpx.Client(timeout=30.0) as client:
        headers, ws_id = _login(client, base_url)
        doc = (_DATA_DIR / "rag_smoke.md").read_bytes()
        upload = client.post(
            f"{base_url}/api/knowledge/files",
            headers=headers,
            params={"workspace_id": ws_id},
            files={"file": ("rag_smoke.md", doc, "text/markdown")},
        )
        upload.raise_for_status()
        file_id = _wait_embedded(client, base_url, headers, ws_id)
        print(f"文档已摄取: {file_id}，开始 {len(queries)} 条查询（top_k={top_k}）\n")

        hits_count = 0
        mrr_sum = 0.0
        misses: list[str] = []
        for item in queries:
            resp = client.post(
                f"{base_url}/api/knowledge/search",
                headers=headers,
                params={"workspace_id": ws_id},
                json={"query": item["query"], "top_k": top_k},
            )
            resp.raise_for_status()
            results = resp.json()["hits"]
            rank = next(
                (
                    index + 1
                    for index, hit in enumerate(results)
                    if item["expect_substr"] in hit["content"]
                ),
                None,
            )
            status = f"rank={rank}" if rank else "MISS"
            print(f"  [{status:>8}] {item['query']}")
            if rank:
                hits_count += 1
                mrr_sum += 1.0 / rank
            else:
                misses.append(item["query"])

        total = len(queries)
        recall = hits_count / total
        mrr = mrr_sum / total
        print(f"\nRecall@{top_k} = {hits_count}/{total} = {recall:.1%}")
        print(f"MRR       = {mrr:.3f}")
        if misses:
            print("未命中：")
            for query in misses:
                print(f"  - {query}")
    return 0 if not misses else 1


def main() -> None:
    """命令行入口（参数解析后执行评测）。"""
    parser = argparse.ArgumentParser(description="RAG Recall@K 冒烟评测")
    parser.add_argument("--base-url", default="http://localhost:8100", help="服务根地址")
    parser.add_argument("--top-k", type=int, default=5, help="检索条数（默认 5）")
    args = parser.parse_args()
    sys.exit(run(args.base_url, args.top_k))


if __name__ == "__main__":
    main()
