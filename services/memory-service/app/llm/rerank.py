"""Rerank 客户端封装（M-07 RAG 精排；本地 infinity /rerank，Cohere 兼容协议）。

设计要点（与 EmbeddingClient 同风格）：
  - POST {base_url}/rerank，body {model, query, documents}，响应按 index 还原顺序
  - 超时/5xx 重试；失败抛 RerankError，调用方降级（RRF 融合分直排），不阻断检索
  - base_url 默认复用 embedding 服务（同一 infinity 实例双模型路由）
"""

import logging
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class RerankError(Exception):
    """Rerank 服务调用失败（未配置/超时/协议异常），调用方降级处理。"""


class RerankClient:
    """Cohere/Jina 风格 /rerank 客户端（进程内复用）。"""

    def __init__(self, base_url: str, model: str) -> None:
        """初始化客户端。

        Args:
            base_url: rerank 服务地址（如 http://embedding:7997）。
            model: 模型标识（infinity 为模型路径或注册名）。
        """
        self._base_url = base_url.rstrip("/")
        self._model = model

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        """对 documents 按 query 相关性打分。

        Args:
            query: 用户查询。
            documents: 候选片段正文。

        Returns:
            与输入等长等序的相关性分数（越高越相关）。

        Raises:
            RerankError: 上游失败或响应结构异常。
        """
        if not documents:
            return []
        settings = get_settings()
        payload: dict[str, Any] = {"model": self._model, "query": query, "documents": documents}
        last_error: Exception | None = None
        for _attempt in range(settings.rerank_max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=settings.rerank_timeout_seconds) as client:
                    resp = await client.post(f"{self._base_url}/rerank", json=payload)
                if resp.status_code >= 500:
                    last_error = RerankError(f"上游 {resp.status_code}: {resp.text[:200]}")
                    continue
                if resp.status_code != 200:
                    raise RerankError(f"上游拒绝 {resp.status_code}: {resp.text[:200]}")
                return self._parse_response(resp.json(), expected_len=len(documents))
            except (httpx.HTTPError, ValueError) as exc:  # 超时/连接失败/响应非 JSON
                last_error = RerankError(f"网络或解析错误: {exc}")
        raise last_error or RerankError("未知 rerank 错误")

    @staticmethod
    def _parse_response(data: dict[str, Any], *, expected_len: int) -> list[float]:
        """解析响应：兼容 results（Cohere/Jina）与 data（部分网关）两种键名。

        Raises:
            RerankError: 响应结构异常或条数不足。
        """
        items = data.get("results") or data.get("data")
        if not isinstance(items, list):
            raise RerankError(f"响应格式异常: 缺少 results/data 键 {str(data)[:200]}")
        # None 表示该位置上游未返回（正常协议应全量返回），全量校验后再转换
        scores: list[float | None] = [None] * expected_len
        try:
            for item in items:
                index = int(item["index"])
                if index >= expected_len:
                    continue
                scores[index] = float(item["relevance_score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RerankError(f"响应结构异常: {exc}") from exc
        if any(s is None for s in scores):
            raise RerankError("上游返回条数不足")
        assert all(s is not None for s in scores)  # 供类型收窄
        return list(scores)


# ---- 进程级单例与依赖注入 ----

_rerank_client: RerankClient | None = None


def get_rerank_client() -> RerankClient:
    """返回进程级 Rerank 客户端（base_url 默认复用 embedding 服务）。

    Raises:
        RerankError: embedding 与 rerank 均未配置（调用方降级 RRF 直排）。
    """
    global _rerank_client
    if _rerank_client is None:
        settings = get_settings()
        base_url = settings.rerank_base_url or settings.embedding_base_url
        if not base_url:
            raise RerankError("Rerank 未配置（EMBEDDING_BASE_URL / RERANK_BASE_URL 均为空）")
        _rerank_client = RerankClient(base_url=base_url, model=settings.rerank_model)
    return _rerank_client


def reset_rerank_client() -> None:
    """清空客户端缓存（配置变更/测试重置时调用）。"""
    global _rerank_client
    _rerank_client = None
