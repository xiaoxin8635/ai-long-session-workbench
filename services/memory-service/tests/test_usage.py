"""M-11 用量统计测试（docs/06 §13 / docs/05 §7.2 看板数据源）。

覆盖：
  - chat 每轮落 token_usages：turn_no 递增、上游 usage 优先、流式本地估算
  - 区块明细与 M-05 装配 sections 一致（服务层直测：注入摘要后 memory_tokens > 0）
  - summary 聚合：totals/sessions/turns/by_day、时间窗口过滤
  - 鉴权：401 / 403 / days 越界 422
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from app.context.tokenizer import count_tokens
from app.core.deps import get_redis
from app.db.session import get_session_factory
from app.llm.client import LLMUsage, StreamEvent, StreamTurn
from app.memory.working_memory import WorkingMemory
from app.models.enums import MemberRole
from app.models.observability import TokenUsage
from app.repositories import session_repo, user_repo, workspace_repo
from app.schemas.chat import ChatCompletionRequest, ChatMessage, ChatMetadata
from app.services.chat_service import ChatService
from app.services.session_service import SessionService

_PASSWORD = "passw0rd123"


class FakeLLM:
    """可编程 LLM 假实现（工具协议轮的 usage 可配置，默认上游 10/8）。

    M-08 M3 起非流式对话也走 Agent 图（stream_chat_with_tools），
    complete() 仅作接口完备保留。
    """

    def __init__(
        self,
        reply: str = "这是 EchoDesk 的测试回答。",
        stream_usage: LLMUsage | None = None,
    ) -> None:
        """Args:
        reply: 预设回答文本。
        stream_usage: 工具协议轮上游 usage；LLMUsage() 零值 → 走本地估算，
            缺省为 10/8（上游 usage 优先语义）。
        """
        self.reply = reply
        self.stream_usage = stream_usage or LLMUsage(prompt_tokens=10, completion_tokens=8)

    async def stream_chat(self, messages: list[dict[str, str]]) -> Any:
        """流式：整段一次产出（不携带 usage）。"""
        yield self.reply

    async def stream_chat_with_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any:
        """工具协议流式：与 stream_chat 同正文，携带可配置 usage。"""
        yield StreamEvent(type="delta", text=self.reply)
        yield StreamEvent(
            type="done",
            turn=StreamTurn(content=self.reply, tool_calls=[], usage=self.stream_usage),
        )

    async def complete(self, messages: list[dict[str, str]]) -> tuple[str, LLMUsage]:
        """非流式：返回完整回答与固定 usage。"""
        return self.reply, LLMUsage(prompt_tokens=10, completion_tokens=8)


@pytest.fixture
def usage_client(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    """将路由内 get_llm_client 替换为 FakeLLM 的测试客户端。"""
    fake = FakeLLM()
    monkeypatch.setattr("app.api.routes.chat.get_llm_client", lambda: fake)
    return client


async def _auth_setup(client: AsyncClient, username: str) -> tuple[dict, dict]:
    """注册登录并取个人 workspace，返回 (headers, ws)。"""
    await client.post("/api/auth/register", json={"username": username, "password": _PASSWORD})
    resp = await client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    ws = (await client.get("/api/workspaces", headers=headers)).json()[0]
    return headers, ws


def _body(ws_id: str, content: str, session_id: str | None = None, stream: bool = False) -> dict:
    """构造 chat 请求体（默认非流式）。"""
    metadata: dict[str, Any] = {"workspace_id": ws_id}
    if session_id:
        metadata["session_id"] = session_id
    return {
        "messages": [{"role": "user", "content": content}],
        "stream": stream,
        "metadata": metadata,
    }


async def test_chat_records_usage_per_turn(usage_client: AsyncClient) -> None:
    """非流式两轮：token_usages 各一条、turn_no 递增、上游 usage 优先于本地估算。"""
    headers, ws = await _auth_setup(usage_client, "usage_turns")
    first = await usage_client.post(
        "/v1/chat/completions", headers=headers, json=_body(ws["id"], "第一轮问题")
    )
    assert first.status_code == 200
    sid = first.json()["metadata"]["session_id"]
    second = await usage_client.post(
        "/v1/chat/completions", headers=headers, json=_body(ws["id"], "第二轮问题", session_id=sid)
    )
    assert second.status_code == 200

    db = get_session_factory()()
    try:
        rows = (
            (
                await db.execute(
                    select(TokenUsage)
                    .where(TokenUsage.session_id == uuid.UUID(sid))
                    .order_by(TokenUsage.turn_no)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert [r.turn_no for r in rows] == [1, 2]
        # 非流式：FakeLLM 上游 usage（10/8）优先于本地估算
        assert all(r.prompt_tokens == 10 and r.completion_tokens == 8 for r in rows)
        assert all(r.cost_usd == 0 for r in rows)
    finally:
        await db.close()


async def test_stream_turn_estimates_locally(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """流式（上游 usage 零值）：本地启发式估算 prompt/completion（均 > 0）。"""
    fake = FakeLLM(stream_usage=LLMUsage())  # 零 usage → record_turn 走本地估算
    monkeypatch.setattr("app.api.routes.chat.get_llm_client", lambda: fake)
    headers, ws = await _auth_setup(client, "usage_stream")
    resp = await client.post(
        "/v1/chat/completions", headers=headers, json=_body(ws["id"], "流式问题", stream=True)
    )
    assert resp.status_code == 200
    assert resp.text.rstrip().endswith("data: [DONE]")
    sid = json.loads(
        [ln for ln in resp.text.splitlines() if ln.startswith("data: ")][0][len("data: ") :]
    )["metadata"]["session_id"]

    db = get_session_factory()()
    try:
        row = (
            await db.execute(select(TokenUsage).where(TokenUsage.session_id == uuid.UUID(sid)))
        ).scalar_one()
        # 本地估算：prompt ≥ system 提示 token；completion = 回答 token
        assert row.prompt_tokens >= count_tokens("你是 EchoDesk")
        assert row.completion_tokens == count_tokens(FakeLLM().reply)
    finally:
        await db.close()


async def test_memory_tokens_follow_assembled_sections() -> None:
    """区块明细与装配一致：注入滚动摘要后 memory_tokens > 0（episodic 区块有量）。

    服务层直测（db 直建数据 + ChatService.complete_answer），避免 API 层
    反查用户的绕路；extract_hook=None 隔离后台抽取。
    """
    db = get_session_factory()()
    try:
        user = await user_repo.create(
            db, username=f"usg{uuid.uuid4().hex[:8]}", password_hash="x" * 60
        )
        ws = await workspace_repo.create(db, name="用量区块测试", owner_id=user.id)
        await workspace_repo.add_member(db, ws_id=ws.id, user_id=user.id, role=MemberRole.OWNER)
        created = await SessionService().create(db, ws_id=ws.id, user=user, title="用量区块")
        session = await session_repo.get_by_id(db, workspace_id=ws.id, session_id=created.id)
        assert session is not None
        session.rolling_summary = "用户此前在测试会话讨论过区块明细口径。"
        await db.commit()

        payload = ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="继续")],
            stream=False,
            metadata=ChatMetadata(workspace_id=str(ws.id), session_id=str(created.id)),
        )
        service = ChatService(FakeLLM(), WorkingMemory(get_redis()), extract_hook=None)
        await service.complete_answer(
            db,
            ws_id=ws.id,
            user_id=user.id,
            session_id=created.id,
            payload=payload,
            user_msg_id=uuid.uuid4(),
        )

        row = (
            await db.execute(select(TokenUsage).where(TokenUsage.session_id == created.id))
        ).scalar_one()
        assert row.prompt_tokens == 10  # 上游 usage 优先
        # 摘要注入 episodic 区块 → memory_tokens > 0；无 RAG/工具注入为 0
        assert row.memory_tokens > 0
        assert row.rag_tokens == 0
        assert row.tool_tokens == 0
    finally:
        await db.close()


async def test_summary_endpoint_aggregates(usage_client: AsyncClient) -> None:
    """summary：totals/by_day/sessions/turns 与库一致；窗口外数据不计入。"""
    headers, ws = await _auth_setup(usage_client, "usage_sum")
    first = await usage_client.post(
        "/v1/chat/completions", headers=headers, json=_body(ws["id"], "聚合第一轮")
    )
    sid = first.json()["metadata"]["session_id"]
    await usage_client.post(
        "/v1/chat/completions", headers=headers, json=_body(ws["id"], "聚合第二轮", session_id=sid)
    )

    db = get_session_factory()()
    try:
        # 造一条窗口外的旧数据（created_at 挪到 30 天前）
        stale = TokenUsage(
            workspace_id=uuid.UUID(ws["id"]),
            session_id=uuid.UUID(sid),
            turn_no=0,
            prompt_tokens=999,
            completion_tokens=999,
        )
        db.add(stale)
        await db.flush()
        await db.execute(
            update(TokenUsage)
            .where(TokenUsage.id == stale.id)
            .values(created_at=datetime.now(UTC) - timedelta(days=30))
        )
        await db.commit()

        resp = await usage_client.get(
            f"/api/usage/summary?workspace_id={ws['id']}&days=7", headers=headers
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["workspace_id"] == ws["id"]
        assert data["days"] == 7
        assert data["turns"] == 2  # 窗口外 turn_no=0 不计入
        assert data["sessions"] == 1
        assert data["totals"]["prompt_tokens"] == 20  # 2 轮 × 上游 usage 10
        assert data["totals"]["completion_tokens"] == 16
        assert data["totals"]["total_tokens"] == 36
        assert len(data["by_day"]) == 1
        assert data["by_day"][0]["prompt_tokens"] == 20
        assert data["by_day"][0]["day"] == datetime.now(UTC).date().isoformat()
    finally:
        await db.close()


async def test_summary_auth_guards(usage_client: AsyncClient) -> None:
    """summary 鉴权：401 无令牌 / 403 跨 workspace / days 越界 422。"""
    headers, ws = await _auth_setup(usage_client, "usage_guard")
    other_headers, _ = await _auth_setup(usage_client, "usage_other")

    no_token = await usage_client.get(f"/api/usage/summary?workspace_id={ws['id']}")
    assert no_token.status_code == 401
    cross = await usage_client.get(
        f"/api/usage/summary?workspace_id={ws['id']}", headers=other_headers
    )
    assert cross.status_code == 403
    bad_days = await usage_client.get(
        f"/api/usage/summary?workspace_id={ws['id']}&days=0", headers=headers
    )
    assert bad_days.status_code == 422
