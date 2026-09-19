"""M-13 LLM-as-judge（docs/01 §8.2 的 Context Precision 与 Hallucination Rate）。

设计原则：
  - 走 memory-service 无状态任务端点 /v1/tasks/completions（不落库、不污染
    评测会话历史；调用链路自动进 Langfuse 观测，可事后审计 judge 行为）
  - rubric 固定写死在 prompt 中，输出要求 JSON，保证可复现
  - 解析失败/上游异常降级为 {"ok": False, "error": ...}，聚合时从分母剔除
    （见 metrics.aggregate_judge），不污染指标

两项指标：
  - Context Precision：注入上下文对回答"有贡献"的条目占比（judge 逐探针判定）
  - Hallucination Rate：回答中无依据关键论断的比例（judge 抽查判定）
"""

import json
import re
from dataclasses import dataclass

import httpx

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class JudgeContext:
    """judge 调用所需的服务端上下文。

    Attributes:
        base_url: memory-service 根地址（如 http://localhost:8100）。
        token: 登录 JWT（Bearer）。
        workspace_id: 工作区 ID（任务端点成员校验必填）。
    """

    base_url: str
    token: str
    workspace_id: str


# ---- Context Precision：注入上下文对回答的贡献度 ----

_CONTEXT_PRECISION_PROMPT = """你是一个严格的上下文质量评审员。评测系统向对话助手注入了\
若干条背景记忆，现在需要判断这些记忆对最终回答是否真的有贡献。

【用户问题】
{question}

【注入的记忆条目】（编号从 1 开始）
{memory_list}

【助手回答】
{answer}

请逐条判断每条记忆对回答是否有贡献（被回答实际引用/依赖其信息/支撑措辞）。
只输出 JSON，不要输出其他内容，格式：
{{"contributed": [有贡献的条目编号], "total": {total}}}
"""


async def judge_context_precision(
    ctx: JudgeContext, question: str, memories: list[str], answer: str
) -> dict:
    """判定注入记忆对回答的贡献比例（Context Precision 原始输出）。

    Args:
        ctx: 服务端上下文。
        question: 探针问题。
        memories: 本次注入的记忆条目文本列表。
        answer: 助手回答。

    Returns:
        {"ok": bool, "contributed_ratio": float, "error": str|None}；
        解析失败/上游异常时 ok=False 且 contributed_ratio=0.0。
    """
    if not memories:
        return {"ok": True, "contributed_ratio": 1.0, "error": None}  # 无注入无从失效
    memory_list = "\n".join(f"{i}. {m}" for i, m in enumerate(memories, start=1))
    prompt = _CONTEXT_PRECISION_PROMPT.format(
        question=question, memory_list=memory_list, answer=answer, total=len(memories)
    )
    raw = await _ask_llm(ctx, prompt)
    if raw is None:
        return {"ok": False, "contributed_ratio": 0.0, "error": "llm_unavailable"}
    try:
        parsed = _extract_json(raw)
        contributed = parsed["contributed"]
        total = parsed.get("total") or len(memories)
        ratio = len(contributed) / total if total else 1.0
        return {"ok": True, "contributed_ratio": round(min(max(ratio, 0.0), 1.0), 4), "error": None}
    except (KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "contributed_ratio": 0.0, "error": f"parse_failed:{exc}"}


# ---- Hallucination：无依据关键论断 ----

_HALLUCINATION_PROMPT = """你是一个严格的事实性评审员。判断下面助手的回答中，\
是否存在"无依据的关键论断"（回答声称了具体事实/数字/承诺，但既不是用户问题给出的，\
也不是注入背景中出现的，也非常识推理可得）。

【用户问题】
{question}

【助手回答】
{answer}

只输出 JSON，不要输出其他内容，格式：
{{"unsupported_claims": [无依据论断的原文摘录], "has_unsupported": false}}
"""


async def judge_hallucination(ctx: JudgeContext, question: str, answer: str) -> dict:
    """判定回答的无依据论断情况（Hallucination 原始输出）。

    Args:
        ctx: 服务端上下文。
        question: 探针问题。
        answer: 助手回答。

    Returns:
        {"ok": bool, "has_unsupported": bool, "error": str|None}。
    """
    prompt = _HALLUCINATION_PROMPT.format(question=question, answer=answer)
    raw = await _ask_llm(ctx, prompt)
    if raw is None:
        return {"ok": False, "has_unsupported": False, "error": "llm_unavailable"}
    try:
        parsed = _extract_json(raw)
        return {
            "ok": True,
            "has_unsupported": bool(parsed.get("has_unsupported")),
            "claims": parsed.get("unsupported_claims") or [],
            "error": None,
        }
    except (KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "has_unsupported": False, "error": f"parse_failed:{exc}"}


# ---- 共用底层 ----


async def _ask_llm(ctx: JudgeContext, prompt: str) -> str | None:
    """调用任务端点让 LLM 输出 JSON（异常降级 None，不抛出）。

    Args:
        ctx: 服务端上下文。
        prompt: 完整评审 prompt（要求只输出 JSON）。

    Returns:
        模型原始输出文本；端点不可用/上游异常时 None。
    """
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "metadata": {"workspace_id": ctx.workspace_id},
    }
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                f"{ctx.base_url}/v1/tasks/completions",
                headers={"Authorization": f"Bearer {ctx.token}"},
                json=payload,
            )
            if resp.status_code != 200:
                return None
            return resp.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, ValueError):
        return None


def _extract_json(raw: str) -> dict:
    """从模型输出中提取 JSON 对象（容忍 ```json 包裹与前后的说明文字）。

    Args:
        raw: 模型原始输出。

    Returns:
        解析后的 dict。

    Raises:
        ValueError: 输出中不存在可解析的 JSON 对象。
    """
    match = _JSON_BLOCK_RE.search(raw)
    if not match:
        raise ValueError("no json object in output")
    return json.loads(match.group(0))
