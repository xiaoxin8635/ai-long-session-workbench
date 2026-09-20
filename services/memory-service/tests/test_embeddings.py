"""EmbeddingClient 单元测试（httpx.MockTransport 注入，不依赖真实 embedding 服务）。

覆盖评测优化轮三期修复的分批与重试行为：
  - 大批量输入按 embedding_batch_size 分批发送（325 切片一次性请求会
    超时并占死本地推理队列的实机事故回归）
  - 超时/连接失败重试带退避
  - 重试耗尽后抛 EmbeddingError
"""

from typing import Any

import httpx
import pytest

from app.core.config import get_settings
from app.llm import embeddings as embeddings_module
from app.llm.embeddings import EmbeddingClient, EmbeddingError

# 测试用轻量向量维度（经环境变量与 Settings 对齐）
_TEST_DIM = 4


def _json_loads(request: httpx.Request) -> dict[str, Any]:
    """解析请求 body 为 JSON（mock handler 内部辅助）。"""
    import json

    result: dict[str, Any] = json.loads(request.content.decode("utf-8"))
    return result


def _mock_handler(
    batches: list[list[str]],
    vectors_by_call: list[list[list[float]]] | None = None,
    fail_times: int = 0,
) -> httpx.MockTransport:
    """构造记录式 MockTransport：记录每次请求的 input，按需前 N 次失败。

    Args:
        batches: 出参收集器（每次调用追加该次请求的 input 文本列表）。
        vectors_by_call: 每次调用返回的向量序列；None 时按 index 生成重复特征向量。
        fail_times: 前 N 次调用以连接错误失败（模拟网络故障）。

    Returns:
        注入 EmbeddingClient 的 transport。
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        """按调用序号决定失败或返回 OpenAI 兼容 embeddings 响应。"""
        idx = calls["n"]
        calls["n"] += 1
        payload: dict[str, Any] = _json_loads(request)
        texts: list[str] = payload["input"]
        batches.append(texts)  # 失败请求同样记录（供重试次数断言）
        if idx < fail_times:
            raise httpx.ConnectError("模拟连接失败")
        if vectors_by_call is not None:
            data_vectors = vectors_by_call[idx]
        else:
            data_vectors = [[float(i + 1)] * _TEST_DIM for i in range(len(texts))]
        return httpx.Response(
            200,
            json={"data": [{"index": i, "embedding": v} for i, v in enumerate(data_vectors)]},
        )

    return httpx.MockTransport(handler)


@pytest.fixture()
def _batch_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """测试环境：4 维向量 + 可控批次大小 + 关闭重试退避等待。

    get_settings 为 lru_cache 进程缓存——设完环境变量必须清缓存重建，
    yield 后再清一次使后续测试回到默认配置。
    """
    monkeypatch.setenv("MEMORY_SERVICE_EMBEDDING_DIM", str(_TEST_DIM))
    monkeypatch.setenv("MEMORY_SERVICE_EMBEDDING_BASE_URL", "http://t")
    monkeypatch.setattr(embeddings_module, "_RETRY_BACKOFF_SECONDS", 0.0)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_embed_splits_into_batches(monkeypatch: pytest.MonkeyPatch, _batch_env: None) -> None:
    """大批量输入按 batch_size 分批发送且顺序还原（5 条、批次 2 → 2/2/1）。"""
    monkeypatch.setenv("MEMORY_SERVICE_EMBEDDING_BATCH_SIZE", "2")
    batches: list[list[str]] = []
    client = EmbeddingClient("http://t/", "test-model", transport=_mock_handler(batches))
    texts = ["一", "二", "三", "四", "五"]

    vectors = await client.embed(texts)

    assert [len(b) for b in batches] == [2, 2, 1]
    assert [t for b in batches for t in b] == texts  # 拼接后与输入等序
    assert len(vectors) == 5
    assert all(len(v) == _TEST_DIM for v in vectors)


async def test_embed_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, _batch_env: None
) -> None:
    """连接失败一次后重试成功（重试路径可用，退避不阻塞）。"""
    monkeypatch.setenv("MEMORY_SERVICE_EMBEDDING_BATCH_SIZE", "4")
    batches: list[list[str]] = []
    client = EmbeddingClient(
        "http://t", "test-model", transport=_mock_handler(batches, fail_times=1)
    )

    vectors = await client.embed(["hello"])

    assert len(batches) == 2  # 首次失败 + 重试成功
    assert vectors == [[1.0] * _TEST_DIM]


async def test_embed_raises_after_retries_exhausted(
    monkeypatch: pytest.MonkeyPatch, _batch_env: None
) -> None:
    """持续失败时重试耗尽后抛 EmbeddingError（不再向调用方泄漏 httpx 异常）。"""
    monkeypatch.setenv("MEMORY_SERVICE_EMBEDDING_BATCH_SIZE", "4")
    batches: list[list[str]] = []
    client = EmbeddingClient(
        "http://t", "test-model", transport=_mock_handler(batches, fail_times=99)
    )

    with pytest.raises(EmbeddingError, match="网络错误"):
        await client.embed(["hello"])
    # 重试耗尽：默认 embedding_max_retries=1 → 共 1+2 次请求全部失败
    assert len(batches) == 2


async def test_embed_empty_input_short_circuit(_batch_env: None) -> None:
    """空输入直接返回空列表（不发起任何请求）。"""
    client = EmbeddingClient("http://t", "test-model", transport=_mock_handler([]))
    assert await client.embed([]) == []
