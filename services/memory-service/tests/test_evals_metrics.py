"""M-13 评测组件单测：metrics 指标计算器 + judges 解析容错 + 数据集格式校验。

覆盖三类：
  1. metrics.py 纯函数的边界与口径（Recall@5 的 top-k/全量覆盖、Precision
     陷阱口径、Conflict 并存、percentile 线性插值、aggregate_judge 剔错）
  2. judges.py 的 JSON 提取容错与无注入短路（不发起真实 LLM 调用）
  3. run_eval.load_jsonl 对四个数据集的必填字段校验（防手写扩充时漏字段）
"""

import asyncio
import sys
from pathlib import Path

# evals/ 与 tests/ 平级，手动加入 sys.path 以导入评测组件
EVALS_DIR = Path(__file__).resolve().parents[1] / "evals"
sys.path.insert(0, str(EVALS_DIR))

import judges  # noqa: E402
import metrics  # noqa: E402
import run_eval  # noqa: E402

# ---- metrics.parse_injected_memories ----


def test_parse_injected_memories_extracts_items():
    """preview messages 中的 <memory> 条目应解析出 type/source/content。"""
    messages = [
        {
            "role": "system",
            "content": '<procedural_memories><memory type="procedural" source="extract:1">'
            "偏好简洁回答</memory></procedural_memories>"
            '<semantic_memories><memory type="semantic" source="task:9">'
            "任务「复习索引」进行中</memory></semantic_memories>",
        },
        {"role": "user", "content": "我的复习重点是什么？"},
    ]
    items = metrics.parse_injected_memories(messages)
    assert len(items) == 2
    assert items[0].type == "procedural"
    assert items[0].source == "extract:1"
    assert items[0].content == "偏好简洁回答"
    assert items[1].type == "semantic"
    assert items[1].source == "task:9"


def test_parse_injected_memories_empty():
    """无结构化标签时返回空列表，不抛错。"""
    assert metrics.parse_injected_memories([{"role": "user", "content": "你好"}]) == []


# ---- metrics.recall_at_5 ----


def _item(content: str) -> metrics.InjectedItem:
    """构造测试用注入条目。"""
    return metrics.InjectedItem(type="semantic", source="extract", content=content)


def test_recall_at_5_full_coverage_in_top5():
    """期望关键词组被 top-5 内同一条目全量覆盖时命中。"""
    probes = [
        (
            [_item(f"杂项{i}") for i in range(4)] + [_item("目标岗位 后端 基础架构")],
            ["后端", "基础架构"],
        )
    ]
    assert metrics.recall_at_5(probes) == 1.0


def test_recall_at_5_rank6_miss():
    """覆盖条目排在第 6 条（k=5 之外）不计命中。"""
    probes = [([_item(f"杂项{i}") for i in range(5)] + [_item("目标岗位 后端")], ["后端"])]
    assert metrics.recall_at_5(probes) == 0.0


def test_recall_at_5_cross_item_not_counted():
    """关键词分散在两条目（拼凑）不计命中，防止跨条目误判。"""
    probes = [([_item("岗位是后端"), _item("方向基础架构")], ["后端", "基础架构"])]
    assert metrics.recall_at_5(probes) == 0.0


def test_recall_at_5_empty():
    """无探针时返回 0.0。"""
    assert metrics.recall_at_5([]) == 0.0


def test_recall_at_5_procedural_front_does_not_displace_semantic():
    """fix_v9_mh 回归：procedural 全局条目霸占渲染前 5 不挤占判定窗口。

    取证实锤：装配渲染 procedural 先于 semantic，目标 semantic 条目
    （检索 sim 第 1）被挤到渲染第 6-8 位——按渲染顺序切 [:5] 判 6/20
    假 FAIL。判定窗口只取 semantic 条目后此类场景应命中。
    """
    probes = [
        (
            [_item(f"全局日程{i}") for i in range(4)]
            + [metrics.InjectedItem(type="procedural", source="extract", content="每周四面试")]
            + [_item("GitHub 用户名 cm-hust")],
            ["cm-hust"],
        )
    ]
    assert metrics.recall_at_5(probes) == 1.0


def test_recall_at_5_semantic_beyond_top5_still_miss():
    """semantic 条目自身排在 semantic 桶第 6 位仍不计命中（口径不放宽）。"""
    probes = [
        (
            [
                metrics.InjectedItem(type="procedural", source="extract", content=f"偏好{i}")
                for i in range(5)
            ]
            + [_item(f"无关事实{i}") for i in range(5)]
            + [_item("目标岗位 后端")],
            ["后端"],
        )
    ]
    assert metrics.recall_at_5(probes) == 0.0


# ---- metrics.memory_precision / conflict_rate / consistency ----


def test_memory_precision_trap_counted_invalid():
    """陷阱关键词组出现在注入条目中记无效；无注入返回 1.0。"""
    probes = [
        ([_item("期望岗位 前端 Vue"), _item("城市杭州")], [["前端", "Vue"]]),
        ([_item("城市北京")], []),
    ]
    assert metrics.memory_precision(probes) == 1.0 - 1 / 3
    assert metrics.memory_precision([([], [])]) == 1.0


def test_conflict_rate_coexist():
    """新旧事实以两条独立条目并存才计入；仅新事实不计。"""
    coexist = ([_item("旧号 138"), _item("新号 139")], "138", "139")
    only_new = ([_item("新号 139")], "138", "139")
    assert metrics.conflict_rate([coexist, only_new]) == 0.5
    assert metrics.conflict_rate([]) == 0.0


def test_conflict_rate_merged_rewrite_not_coexist():
    """Fix 轮口径：supersede 后新条目以背景口吻同现新旧值不算并存。

    Fix 轮实测：ACTIVE 新条 content 如"当前手机号 139…，旧号 138…已注销"
    属良性上下文改写，旧口径误判为并存（6 行 probe 误判 4 行）。
    """
    merged = ([_item("用户当前手机号是139，旧号138已准备注销")], "138", "139")
    assert metrics.conflict_rate([merged]) == 0.0


def test_conflict_rate_stale_old_with_new_is_coexist():
    """旧条未被替代（仅含旧值）且新条已入库（仅含新值）仍判并存。"""
    stale = (
        [_item("用户的手机号是138"), _item("用户当前手机号是139")],
        "138",
        "139",
    )
    assert metrics.conflict_rate([stale]) == 1.0


def test_consistency_zero_total():
    """total=0 时返回 0.0（无断言不算满分）。"""
    assert metrics.consistency(0, 0) == 0.0
    assert metrics.consistency(3, 4) == 0.75


# ---- metrics.token_savings / percentile / aggregate_judge ----


def test_token_savings_baseline_guard():
    """基线非正时返回 0.0（防除零/负口径）。"""
    assert metrics.token_savings(100.0, 0.0) == 0.0
    assert metrics.token_savings(400.0, 1000.0) == 0.6


def test_percentile_linear_interpolation():
    """线性插值百分位：4 样本 p50 等于中间两值均值，空列表 0.0。"""
    assert metrics.percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert metrics.percentile([5.0], 95) == 5.0
    assert metrics.percentile([], 95) == 0.0
    assert metrics.percentile([10.0, 20.0], 95) == 19.5


def test_aggregate_judge_errors_excluded():
    """error 条目从分母剔除；类型异常值剔除；空列表返回 (0.0, 0)。"""
    scores = [
        {"ok": True, "contributed_ratio": 0.8, "error": None},
        {"ok": False, "contributed_ratio": 0.0, "error": "llm_unavailable"},
        {"ok": True, "contributed_ratio": 1.0, "error": None},
    ]
    avg, n = metrics.aggregate_judge(scores, "contributed_ratio")
    assert n == 2
    assert avg == 0.9
    assert metrics.aggregate_judge([], "contributed_ratio") == (0.0, 0)


# ---- judges：JSON 提取容错与短路 ----


def test_extract_json_tolerates_wrapping():
    """容忍 ```json 包裹与前后说明文字；无 JSON 抛 ValueError。"""
    assert judges._extract_json('好的，结果：{"contributed": [1], "total": 2}') == {
        "contributed": [1],
        "total": 2,
    }
    assert judges._extract_json('```json\n{"has_unsupported": false}\n```') == {
        "has_unsupported": False
    }
    try:
        judges._extract_json("完全没有 JSON 的输出")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_judge_context_precision_no_memories_short_circuit():
    """无注入记忆时直接返回满分 1.0，不发起 LLM 调用（base_url 故意不可达）。"""
    ctx = judges.JudgeContext(base_url="http://127.0.0.1:1", token="t", workspace_id="w")
    result = asyncio.run(judges.judge_context_precision(ctx, "问题", [], "回答"))
    assert result == {"ok": True, "contributed_ratio": 1.0, "error": None}


def test_judge_hallucination_llm_unavailable_degrades():
    """端点不可达时降级为 ok=False（不抛出），聚合侧从分母剔除。"""
    ctx = judges.JudgeContext(base_url="http://127.0.0.1:1", token="t", workspace_id="w")
    result = asyncio.run(judges.judge_hallucination(ctx, "问题", "回答"))
    assert result["ok"] is False
    assert result["error"] == "llm_unavailable"


def test_judge_hallucination_prompt_includes_memories(monkeypatch):
    """传入注入记忆时 prompt 必须包含记忆文本（judge 缺依据来源会系统性误判幻觉）。"""
    captured: dict[str, str] = {}

    async def fake_ask(ctx: judges.JudgeContext, prompt: str) -> str:
        """替换 _ask_llm：捕获 prompt 并返回固定合法 JSON。"""
        captured["prompt"] = prompt
        return '{"unsupported_claims": [], "has_unsupported": false}'

    monkeypatch.setattr(judges, "_ask_llm", fake_ask)
    ctx = judges.JudgeContext(base_url="http://127.0.0.1:1", token="t", workspace_id="w")
    result = asyncio.run(
        judges.judge_hallucination(
            ctx,
            "我的手机号是多少？",
            "你的手机号是 13812345678。",
            ["用户的手机号是 13812345678"],
        )
    )
    assert result == {"ok": True, "has_unsupported": False, "claims": [], "error": None}
    assert "13812345678" in captured["prompt"]


def test_judge_hallucination_prompt_without_memories_placeholder(monkeypatch):
    """memories 为 None 时 prompt 用占位文案标明无注入（不误暗示评审员找依据）。"""
    captured: dict[str, str] = {}

    async def fake_ask(ctx: judges.JudgeContext, prompt: str) -> str:
        """替换 _ask_llm：捕获 prompt 并返回固定合法 JSON。"""
        captured["prompt"] = prompt
        return '{"unsupported_claims": [], "has_unsupported": false}'

    monkeypatch.setattr(judges, "_ask_llm", fake_ask)
    ctx = judges.JudgeContext(base_url="http://127.0.0.1:1", token="t", workspace_id="w")
    asyncio.run(judges.judge_hallucination(ctx, "问题", "回答"))
    assert "（无注入记忆）" in captured["prompt"]


# ---- run_eval：数据集格式与确定性 ----


def test_datasets_all_valid():
    """四个数据集均存在且必填字段齐全（防手工扩充时漏字段）。"""
    for suite in ("long_turn", "memory_hit", "conflict", "task_continuity"):
        rows = run_eval.load_jsonl(suite)
        assert rows, f"{suite} 数据集为空"


def test_ensure_fresh_token_refreshes_when_stale():
    """token 年龄超阈值时走 /api/auth/refresh 换新双令牌；fresh 时短路不发请求。

    背景：access token 有效期 30 分钟而全量评测约 35 分钟，
    中途不刷新会导致后半程全部 401（首跑实测教训）。
    """
    import time as time_mod

    import httpx

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.url.path == "/api/auth/refresh"
        return httpx.Response(
            200, json={"access_token": "new-access", "refresh_token": "new-refresh"}
        )

    ec = run_eval.EvalClient(
        base_url="http://test",
        token="old",
        workspace_id="ws",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5),
        refresh_token="old-refresh",
        _token_ts=time_mod.time() - run_eval.TOKEN_REFRESH_INTERVAL - 1,
    )
    asyncio.run(ec._ensure_fresh_token())
    assert ec.token == "new-access"
    assert ec.refresh_token == "new-refresh"
    assert calls == ["/api/auth/refresh"]
    # 刚刷新过（fresh）应短路，不再发请求
    asyncio.run(ec._ensure_fresh_token())
    assert len(calls) == 1


def test_pick_distractor_deterministic():
    """干扰话术选取确定性（同 seed 同 offset 必同值，跨进程可复现）。"""
    assert run_eval._pick_distractor("mh-01", 0) == run_eval._pick_distractor("mh-01", 0)
    assert run_eval._pick_distractor("mh-01", 0) in run_eval.DISTRACTORS


def test_build_report_single_suite():
    """单套件模式下缺失套件指标记 0、可用指标正常（build/render 纯函数烟雾测试）。"""
    outcome = run_eval.SuiteOutcome(name="task_continuity", samples=4)
    outcome.extra = {"task_continuity": 0.75, "assert_detail": "3/4"}
    report = run_eval.build_report(
        label="smoke",
        outcomes=[outcome],
        token_actual=500.0,
        token_baseline=1000.0,
        ttfb=[],
        e2e=[],
        judge_ctx=[],
        judge_halu=[],
    )
    assert report["metrics"]["task_continuity"] == 0.75
    assert report["metrics"]["memory_recall_at_5"] == 0.0
    assert report["metrics"]["token_savings"] == 0.5
    md = run_eval.render_markdown(report, None)
    assert "| memory_recall_at_5 |" in md and "| task_continuity |" in md
