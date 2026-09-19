"""M-13 评测指标计算器（docs/01 §8.2 九项指标中的规则可计算部分）。

设计原则：
  - 纯函数 + 显式数据结构，不依赖 app 包与外部服务（可独立单测）
  - LLM-as-judge 类指标（Context Precision / Hallucination）见 judges.py，
    本模块只做其输出的聚合
  - 注入条目来源：/api/context/preview 的 messages 中解析出的
    ``<memory type="..." source="...">content</memory>`` 条目（builder 渲染格式）

指标口径（与 docs/01 §8.2 对齐）：
  - Memory Recall@5：期望关键词组命中注入记忆 top-5 的比例（目标 ≥ 80%）
  - Memory Precision：注入条目中非"陷阱事实"的占比（目标 ≥ 70%）
  - Long-turn Consistency：规则断言通过比例（目标 ≥ 90%）
  - Fact Conflict Rate：新旧事实并存于注入上下文的比例（目标 ≤ 10%）
  - Token Cost / Turn：实际均值相对"全量历史塞入"基线的下降比例（目标 ≥ 40%）
  - P95 Latency：首 token / 端到端时延的 95 分位（目标 ≤ 2s / ≤ 8s）
  - Task Continuity：跨会话任务断言通过比例（目标 ≥ 80%）
"""

import math
import re
from dataclasses import dataclass, field

# builder._render_tagged 的记忆条目渲染格式（M-05 固定口径）
MEMORY_ITEM_RE = re.compile(
    r'<memory type="(?P<type>[^"]+)" source="(?P<source>[^"]+)">(?P<content>.*?)</memory>',
    re.DOTALL,
)


@dataclass(frozen=True)
class InjectedItem:
    """一条被注入上下文的记忆条目（从 preview messages 解析）。"""

    type: str  # 区块名（procedural/semantic/...）
    source: str  # 来源（extract/task:<id>/session_summary:<sid>/...）
    content: str  # 记忆正文


@dataclass
class SuiteResult:
    """单个评测套件（suite）的原始采集结果。

    Attributes:
        total_probes: 探针总数（分母）。
        recall_hits: 期望关键词组命中注入 top-5 的次数。
        injected_per_probe: 每次探针注入的条目明细（Precision/Conflict 用）。
        trap_hits: 陷阱事实（与探针无关、不应注入）出现在注入中的次数。
        conflict_coexists: 新旧事实并存于同一探针注入中的次数。
        assert_passed / assert_total: 规则断言（Consistency/任务连续性）计数。
        ttfb_ms / e2e_ms: 每次探针的首 token / 端到端时延采样（毫秒）。
        judge_scores: LLM-as-judge 逐条原始输出（结构见 judges.py）。
    """

    total_probes: int = 0
    recall_hits: int = 0
    injected_per_probe: list[list[InjectedItem]] = field(default_factory=list)
    trap_hits: int = 0
    conflict_coexists: int = 0
    assert_passed: int = 0
    assert_total: int = 0
    ttfb_ms: list[float] = field(default_factory=list)
    e2e_ms: list[float] = field(default_factory=list)
    judge_scores: list[dict] = field(default_factory=list)


def parse_injected_memories(messages: list[dict[str, str]]) -> list[InjectedItem]:
    """从 preview 装配产物（OpenAI messages）解析注入的记忆条目。

    Args:
        messages: /api/context/preview 返回的 messages（system 含结构化标签）。

    Returns:
        按出现顺序（即区块装配顺序）排列的条目列表；无记忆时为空。
    """
    items: list[InjectedItem] = []
    for msg in messages:
        for m in MEMORY_ITEM_RE.finditer(msg.get("content", "")):
            items.append(
                InjectedItem(
                    type=m.group("type"), source=m.group("source"), content=m.group("content")
                )
            )
    return items


def _hit_in_top(injected: list[InjectedItem], keywords: list[str], k: int = 5) -> bool:
    """判定一组期望关键词是否被注入条目 top-k 中任一条目全量覆盖。

    Args:
        injected: 注入条目（顺序即渲染顺序）。
        keywords: 期望关键词组（须同时出现在同一条目中，避免跨条目拼凑误判）。
        k: 取前 k 条参与判定（Recall@k 口径）。

    Returns:
        命中 True；k 条内无全量覆盖 False。
    """
    return any(all(kw in item.content for kw in keywords) for item in injected[:k])


def recall_at_5(
    probes: list[tuple[list[InjectedItem], list[str]]],
) -> float:
    """Memory Recall@5：期望关键词组命中注入记忆 top-5 的比例。

    Args:
        probes: 每次探针的 (注入条目列表, 期望关键词组) 元组列表。

    Returns:
        命中比例 [0,1]；无探针时返回 0.0。
    """
    if not probes:
        return 0.0
    hits = sum(1 for injected, kws in probes if kws and _hit_in_top(injected, kws))
    return hits / len(probes)


def memory_precision(
    probes: list[tuple[list[InjectedItem], list[str]]],
) -> float:
    """Memory Precision：注入条目中"有效"条目的占比。

    陷阱口径：数据集预埋的 trap_facts 关键词若出现在注入条目中，
    该条目记为无效（与探针无关的记忆不应挤占预算）。

    Args:
        probes: 每次探针的 (注入条目列表, 陷阱关键词组) 元组列表。

    Returns:
        有效占比 [0,1]；无注入时返回 1.0（无注入不算无效）。
    """
    total = 0
    invalid = 0
    for injected, traps in probes:
        for item in injected:
            total += 1
            if any(all(kw in item.content for kw in trap) for trap in traps):
                invalid += 1
    if total == 0:
        return 1.0
    return 1.0 - invalid / total


def conflict_rate(probes: list[tuple[list[InjectedItem], str, str]]) -> float:
    """Fact Conflict Rate：新旧事实并存于同一探针注入上下文的比例。

    Args:
        probes: 每次探针的 (注入条目列表, 旧事实关键词, 新事实关键词) 元组。

    Returns:
        并存比例 [0,1]（越低越好）；无探针时 0.0。
    """
    if not probes:
        return 0.0
    coexists = 0
    for injected, old_kw, new_kw in probes:
        has_old = any(old_kw in item.content for item in injected)
        has_new = any(new_kw in item.content for item in injected)
        if has_old and has_new:
            coexists += 1
    return coexists / len(probes)


def consistency(passed: int, total: int) -> float:
    """Long-turn Consistency / Task Continuity 共用的通过率。

    Args:
        passed: 断言通过次数。
        total: 断言总次数。

    Returns:
        通过比例 [0,1]；总数为 0 时 0.0。
    """
    return passed / total if total else 0.0


def token_savings(actual_avg: float, baseline_avg: float) -> float:
    """Token Cost / Turn：实际均值相对全量基线的下降比例。

    Args:
        actual_avg: 实际每轮 token 均值（token_usages 聚合）。
        baseline_avg: "全量历史塞入"基线估算均值（count_tokens 口径）。

    Returns:
        下降比例 [0,1]；基线非正时 0.0。
    """
    if baseline_avg <= 0:
        return 0.0
    return 1.0 - actual_avg / baseline_avg


def percentile(samples: list[float], pct: float) -> float:
    """线性插值百分位数（P95 Latency 用）。

    Args:
        samples: 时延采样（毫秒）。
        pct: 百分位（0-100，如 95）。

    Returns:
        对应百分位值；无采样时 0.0。
    """
    if not samples:
        return 0.0
    ordered = sorted(samples)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100) * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    frac = rank - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def aggregate_judge(judge_scores: list[dict], key: str) -> tuple[float, int]:
    """聚合 LLM-as-judge 逐条输出为单一指标。

    judge 条目结构见 judges.py：{"ok": bool, "value": float, "error": str|None}。
    解析失败的条目（error 非空）从分母剔除，不污染指标。

    Args:
        judge_scores: 逐条 judge 输出。
        key: 取值字段名（如 "contributed_ratio" / "clean_ratio"）。

    Returns:
        (均值, 有效条数)。
    """
    values = [
        j[key] for j in judge_scores if not j.get("error") and isinstance(j.get(key), (int, float))
    ]
    if not values:
        return 0.0, 0
    return sum(values) / len(values), len(values)
