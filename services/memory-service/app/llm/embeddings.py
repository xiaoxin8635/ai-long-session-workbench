"""Embedding 客户端封装（M-04 起：记忆向量写入与检索）。

设计要点：
  - OpenAI 兼容 POST {base_url}/embeddings（本地 infinity 服务与云 API 通用）
  - 批量向量化一次请求；data 按 index 对齐还原顺序
  - 超时/5xx 重试；失败抛 EmbeddingError，调用方降级（记忆写 NULL 向量、
    检索返回空候选），不阻塞主链路
"""

import logging
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Embedding 服务调用失败（未配置/超时/协议异常），调用方降级处理。"""


class EmbeddingClient:
    """OpenAI 兼容 /embeddings 客户端（进程内复用，连接池由 httpx 管理）。"""

    def __init__(self, base_url: str, model: str, api_key: str | None = None) -> None:
        """初始化客户端。

        Args:
            base_url: OpenAI 兼容服务地址（如 http://embedding:7997）。
            model: 模型标识（infinity 为模型路径或注册名，云 API 为模型名）。
            api_key: Bearer 凭证；本地服务无需鉴权可不传。
        """
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化文本列表。

        Args:
            texts: 待向量化文本（顺序保留）。

        Returns:
            与输入等长等序的向量列表。

        Raises:
            EmbeddingError: 上游失败（未配置/网络/协议异常/维度不符）。
        """
        if not texts:
            return []
        settings = get_settings()
        url = f"{self._base_url}/embeddings"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload: dict[str, Any] = {"model": self._model, "input": texts}

        last_error: Exception | None = None
        for _attempt in range(settings.embedding_max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=settings.embedding_timeout_seconds) as client:
                    resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code >= 500:
                    last_error = EmbeddingError(f"上游 {resp.status_code}: {resp.text[:200]}")
                    continue
                if resp.status_code != 200:
                    raise EmbeddingError(f"上游拒绝 {resp.status_code}: {resp.text[:200]}")
                return self._parse_response(resp.json(), expected_len=len(texts))
            except httpx.HTTPError as exc:  # 超时/连接失败
                last_error = EmbeddingError(f"网络错误: {exc}")
        raise last_error or EmbeddingError("未知 embedding 错误")

    @staticmethod
    def _parse_response(data: dict[str, Any], *, expected_len: int) -> list[list[float]]:
        """解析 OpenAI 兼容响应：data 按 index 还原输入顺序并校验维度。

        Raises:
            EmbeddingError: 响应结构异常或维度与配置不符。
        """
        settings = get_settings()
        try:
            items = data["data"]
            vectors: list[list[float] | None] = [None] * expected_len
            for item in items:
                vector = [float(x) for x in item["embedding"]]
                if len(vector) != settings.embedding_dim:
                    raise EmbeddingError(
                        f"向量维度 {len(vector)} 与配置 {settings.embedding_dim} 不符"
                    )
                vectors[item["index"]] = vector
        except EmbeddingError:
            raise
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise EmbeddingError(f"响应格式异常: {exc}") from exc
        if any(v is None for v in vectors):
            raise EmbeddingError("上游返回条数不足")
        return [v for v in vectors if v is not None]


# ---- 进程级单例与依赖注入 ----

_embedding_client: EmbeddingClient | None = None


def get_embedding_client() -> EmbeddingClient:
    """返回进程级 Embedding 客户端（按 Settings 懒创建）。

    Raises:
        EmbeddingError: embedding_base_url 未配置。
    """
    global _embedding_client
    if _embedding_client is None:
        settings = get_settings()
        if not settings.embedding_base_url:
            raise EmbeddingError("Embedding 未配置（MEMORY_SERVICE_EMBEDDING_BASE_URL）")
        _embedding_client = EmbeddingClient(
            base_url=settings.embedding_base_url,
            model=settings.embedding_model,
            api_key=settings.embedding_api_key,
        )
    return _embedding_client


def reset_embedding_client() -> None:
    """清空客户端缓存（配置变更/测试重置时调用）。"""
    global _embedding_client
    _embedding_client = None
