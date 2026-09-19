"""M-12 观测子系统测试（docs/06 §14 DoD 的可本地验证部分）。

覆盖：
  - no-op 降级：测试环境未配置 Langfuse 三项配置 → 客户端为 None，
    span/turn/generation 全部 no-op 不抛错（保 111 基线不破坏的关键保证）
  - snippet 截断：对话正文片段上报截断（防全量复制业务数据）
  - 价目折算：config/pricing.yaml 加载 + cost_usd_for 精确计算 +
    未收录/None 模型记 0（不阻断落库）
  - cost_usd 落库：record_turn 按价目表折算（M-11 遗留恒 0 的补齐）

span 树完整性与 token ±5% 一致性属实机验证（需真实 Langfuse 栈，
见 docs/04 §2.9 与 M-12 收官记录）。
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.models.enums import MemberRole
from app.observability import tracing
from app.observability.pricing import cost_usd_for
from app.repositories import session_repo, usage_repo, user_repo, workspace_repo


@pytest.fixture(autouse=True)
def _reset_tracing() -> None:
    """每用例前后清空 tracing 客户端缓存（防用例间配置串扰）。"""
    tracing.reset_client()
    yield
    tracing.reset_client()


# ---- trace_id 归一（实机踩坑回归：SDK 以 int(x,16) 解析，带连字符即 500）----


def test_resolve_trace_id_hex_normalization() -> None:
    """trace_id 必须归一为 32 位无连字符 hex（uuid4().hex 形态）。

    Langfuse SDK 内部对 trace_id 执行 int(trace_id, 16)，标准带连字符
    UUID 串会抛 ValueError 并使 chat 端点 500——本用例锁定归一行为。
    """
    # 带连字符标准 UUID（DB/trace_id_var 常见形态）→ 去连字符小写 hex
    dashed = str(uuid.uuid4())
    resolved = tracing._resolve_trace_id(dashed)
    assert len(resolved) == 32 and "-" not in resolved
    assert resolved == dashed.replace("-", "")
    # 无来源（None 且无请求上下文）→ 新生成合法 hex
    fresh = tracing._resolve_trace_id(None)
    assert len(fresh) == 32 and "-" not in fresh
    # 非法输入 → 不抛错，回退新 hex
    junk = tracing._resolve_trace_id("not-a-uuid")
    assert len(junk) == 32 and "-" not in junk


# ---- no-op 降级（DoD：未配置不破坏既有链路）----


async def test_noop_when_unconfigured() -> None:
    """三项配置任一为空 → 客户端 None，全部 span 入口 no-op 不抛错。"""
    assert tracing.get_client() is None
    with tracing.turn(session_id="s", user_id="u", tags=["chat"], workspace_id="w"):
        pass
    with tracing.span("context.assemble"):
        pass
    with tracing.span("memory.retrieve") as obs:
        obs.update(metadata={"hits": 0})  # no-op 观察对象：update 可链式调用
    with tracing.generation("qwen-plus", stream=True) as obs:
        obs.update(usage_details={"input": 1, "output": 2})
    with tracing.generation(None):
        pass
    tracing.shutdown_flush()  # 未配置时同样安全


def test_snippet_truncation() -> None:
    """正文片段截断：None → 空串、短文原样、长文截断并标注原始长度。"""
    assert tracing.snippet(None) == ""
    assert tracing.snippet("你好") == "你好"
    out = tracing.snippet("x" * 600, limit=100)
    assert out.startswith("x" * 100)
    assert "已截断" in out and "600" in out


# ---- 价目折算（cost_usd 数据源）----


def test_pricing_known_model() -> None:
    """仓库根价目表被自动探测：qwen-plus 1K in + 1K out 的精确成本。"""
    cost = cost_usd_for("qwen-plus", 1000, 1000)
    assert cost == Decimal("0.000111") + Decimal("0.000278")
    assert cost == Decimal("0.000389")


def test_pricing_unknown_model_zero() -> None:
    """未收录/None 模型成本记 0（估算口径，绝不抛错阻断用量落库）。"""
    assert cost_usd_for(None, 1000, 1000) == Decimal("0")
    assert cost_usd_for("no-such-model", 99999, 99999) == Decimal("0")


async def test_record_turn_computes_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    """DoD 关联：record_turn 落库的 cost_usd 按价目表折算（非恒 0）。"""
    monkeypatch.setattr(get_settings(), "llm_model", "qwen-plus")
    async with get_session_factory()() as db:
        user = await user_repo.create(
            db, username=f"cost{uuid.uuid4().hex[:8]}", password_hash="x" * 60
        )
        ws = await workspace_repo.create(db, name="成本空间", owner_id=user.id)
        await workspace_repo.add_member(db, ws_id=ws.id, user_id=user.id, role=MemberRole.OWNER)
        session = await session_repo.create(
            db, workspace_id=ws.id, user_id=user.id, title="成本会话"
        )
        usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=1000)
        entry = await usage_repo.record_turn(
            db,
            ws_id=ws.id,
            session_id=session.id,
            answer="好的。",
            usage=usage,
        )
        await db.commit()
        assert entry.cost_usd == Decimal("0.000389")
