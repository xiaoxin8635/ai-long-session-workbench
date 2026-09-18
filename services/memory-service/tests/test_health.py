"""M-01 冒烟测试：健康端点与统一错误结构（不依赖 DB/Redis）。"""

from fastapi import FastAPI


async def test_healthz_returns_ok(client) -> None:
    """存活探针：进程可响应即 200。"""
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "memory-service"


async def test_trace_id_header_echoed(client) -> None:
    """TraceID 中间件：透传上游 X-Request-ID 或自动生成并回传。"""
    resp = await client.get("/healthz", headers={"X-Request-ID": "test-trace-123"})
    assert resp.headers["X-Request-ID"] == "test-trace-123"


async def test_trace_id_auto_generated(client) -> None:
    """未携带 Request-ID 时响应头应自动生成非空 trace_id。"""
    resp = await client.get("/healthz")
    assert resp.headers.get("X-Request-ID")


def test_app_factory_builds(app: FastAPI) -> None:
    """应用工厂：标题与路由注册正确（经 OpenAPI schema 验证，兼容惰性路由）。"""
    assert app.title.startswith("EchoDesk memory-service")
    paths = set(app.openapi()["paths"])
    assert "/healthz" in paths
    assert "/readyz" in paths
