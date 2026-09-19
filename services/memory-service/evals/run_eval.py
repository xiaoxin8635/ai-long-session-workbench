"""M-13 评测 runner（docs/06 §15 / docs/01 §8.3）。

回放评测集 → 调用 memory-service 真实链路 → 采集注入上下文与回答 →
计算 9 项指标 → 输出 Markdown + JSON 双格式报告。

用法（在 services/memory-service 目录）：
  python evals/run_eval.py --label default --suite all
  python evals/run_eval.py --label memory_first --suite all \
      --baseline evals/reports/eval_default_xxx.json

数据流（端点签名以 app/api/routes 为准）：
  - 对话回放走 POST /v1/chat/completions（metadata.workspace_id 必填、
    metadata.session_id 复用会话；非流式取 usage，final probe 流式测 ttfb）
  - 注入明细走 POST /api/context/preview（workspace_id/session_id/query 均
    在 body；messages 中解析 <memory> 条目）
  - LLM-as-judge 走 POST /v1/tasks/completions（judges.py，不落库）
  - token 实际值走 GET /api/usage/summary（顶层 turns，totals.prompt_tokens）

隔离性：服务无"创建第二个 workspace"端点，故每次运行注册唯一账号
（evalbot-<label>-<ts>，注册自动附带个人 workspace），不污染真实数据；
账号随评测保留，可事后在面板删除。

token 基线口径（近似，报告注明）：runner 维护的完整对话历史按"1 字符≈1
token 上界"估算每轮全量 prompt；actual 为 token_usages 聚合均值（含
system/记忆/RAG 注入）。两值同为全部轮次混合口径，绝对值仅供参考，
以 --baseline A/B 相对变化为主要读数。
"""

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 同目录 import metrics/judges

from judges import JudgeContext, judge_context_precision, judge_hallucination  # noqa: E402
from metrics import (  # noqa: E402
    aggregate_judge,
    conflict_rate,
    consistency,
    memory_precision,
    parse_injected_memories,
    percentile,
    recall_at_5,
    token_savings,
)

EVALS_DIR = Path(__file__).resolve().parent
DATASETS = EVALS_DIR / "datasets"
REPORTS_DIR = EVALS_DIR / "reports"

# access token 有效期 30 分钟，全量评测约 35 分钟——提前 5 分钟用 refresh 换新
TOKEN_REFRESH_INTERVAL = 25 * 60

# 通用干扰话术（memory_hit/conflict 的 gap 填充，与待测事实无关）
DISTRACTORS = [
    "今天天气不错，适合出去走走。",
    "我中午吃了一家新开的面馆，味道还可以。",
    "最近地铁三号线好像在检修，绕了点路。",
    "我同事养了只猫，今天带了照片来办公室。",
    "晚上打算看点闲书，换换脑子。",
    "小区楼下新开了个便利店，还挺方便。",
    "这周电影档期一般，没什么想看的。",
    "我昨天跑了三公里，状态一般。",
    "朋友推荐了一个播客，通勤听还不错。",
    "周末可能下雨，出行计划得调整下。",
]

# 指标目标值（docs/01 §8.2；direction 由指标名隐含，渲染时区分）
TARGETS: dict[str, tuple[float | None, str]] = {
    "memory_recall_at_5": (0.80, "≥80%"),
    "memory_precision": (0.70, "≥70%"),
    "context_precision": (0.70, "≥70%（judge）"),
    "long_turn_consistency": (0.90, "≥90%"),
    "fact_conflict_rate": (0.10, "≤10%"),
    "hallucination_rate": (0.05, "≤5%（judge）"),
    "token_savings": (0.40, "≥40%"),
    "p95_ttfb_ms": (2000, "首 token ≤2000ms"),
    "p95_e2e_ms": (8000, "端到端 ≤8000ms"),
    "task_continuity": (0.80, "≥80%"),
}


# ---- 数据加载与校验 ----

_DATASET_REQUIRED: dict[str, list[str]] = {
    "long_turn": ["group_id", "turns", "final_probe"],
    "memory_hit": ["fact_id", "fact_turn", "gap", "probe", "expected"],
    "conflict": [
        "conflict_id",
        "old_turn",
        "new_turn",
        "gap",
        "probe",
        "old_kw",
        "new_kw",
        "expected",
    ],
    "task_continuity": ["task_id", "task_title", "session_b_probe", "assert_contains"],
}


def load_jsonl(suite: str) -> list[dict]:
    """加载并校验一个评测集（缺必填字段直接报错退出）。

    Args:
        suite: 套件名（long_turn/memory_hit/conflict/task_continuity）。

    Returns:
        逐行解析的 dict 列表。

    Raises:
        SystemExit: 文件缺失或字段校验失败。
    """
    path = DATASETS / f"{suite}.jsonl"
    if not path.exists():
        print(f"[FATAL] 数据集缺失: {path}")
        raise SystemExit(2)
    rows: list[dict] = []
    for i, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        missing = [k for k in _DATASET_REQUIRED[suite] if k not in row]
        if missing:
            print(f"[FATAL] {suite}.jsonl 第 {i} 行缺字段: {missing}")
            raise SystemExit(2)
        rows.append(row)
    return rows


def _pick_distractor(seed: str, offset: int) -> str:
    """确定性选取干扰话术（避免内置 hash 随机化，保证回放可复现）。"""
    return DISTRACTORS[(sum(map(ord, seed)) + offset) % len(DISTRACTORS)]


# ---- 服务端客户端 ----


@dataclass
class EvalClient:
    """memory-service HTTP 客户端（评测链路最小封装）。

    Attributes:
        base_url: 服务根地址。
        token: 登录 JWT。
        workspace_id: 评测工作区 ID（注册自带的个人 workspace）。
        client: 复用的 httpx 异步客户端。
        history_chars: 各轮"全量历史字符"累加（token 基线近似，见模块 docstring）。
        chat_calls: chat 调用次数（基线均值分母）。
    """

    base_url: str
    token: str
    workspace_id: str
    client: httpx.AsyncClient
    history_chars: int = 0
    chat_calls: int = 0
    refresh_token: str = ""
    _token_ts: float = 0.0

    @classmethod
    async def create(cls, base_url: str, username: str, password: str, label: str) -> "EvalClient":
        """注册唯一评测账号 → 登录 → 取注册自带的个人 workspace。

        Args:
            base_url: 服务根地址。
            username: 账号名前缀（自动追加 -<label>-<ts> 保证唯一）。
            password: 账号密码。
            label: 本次评测标签（进账号名，保证隔离）。

        Returns:
            已就绪的客户端。

        Raises:
            SystemExit: 注册/登录/取 workspace 任一失败。
        """
        http = httpx.AsyncClient(timeout=180)
        safe_label = "".join(c if c.isalnum() or c in "_-" else "x" for c in label)
        account = f"{username}-{safe_label}-{int(time.time())}"[:64]
        reg = await http.post(
            f"{base_url}/api/auth/register", json={"username": account, "password": password}
        )
        if reg.status_code not in (201, 409):  # 409=同秒重跑撞名，继续登录即可
            print(f"[FATAL] 注册失败 {reg.status_code}: {reg.text[:200]}")
            raise SystemExit(2)
        login = await http.post(
            f"{base_url}/api/auth/login", json={"username": account, "password": password}
        )
        if login.status_code != 200:
            print(f"[FATAL] 登录失败 {login.status_code}: {login.text[:200]}")
            raise SystemExit(2)
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        ws_list = await http.get(f"{base_url}/api/workspaces", headers=headers)
        ws_list.raise_for_status()
        workspaces = ws_list.json()
        if not workspaces:
            print("[FATAL] 注册后未发现个人 workspace")
            raise SystemExit(2)
        workspace_id = workspaces[0]["id"]
        print(f"eval account={account} workspace={workspace_id}")
        return cls(
            base_url=base_url,
            token=token,
            workspace_id=workspace_id,
            client=http,
            refresh_token=login.json()["refresh_token"],
            _token_ts=time.time(),
        )

    async def _ensure_fresh_token(self) -> None:
        """token 年龄超过阈值时用 refresh 换新（全量评测时长超过 access 有效期）。

        Raises:
            RuntimeError: 刷新请求失败（评测无法继续，明确报错优于逐条 401）。
        """
        if time.time() - self._token_ts < TOKEN_REFRESH_INTERVAL:
            return
        resp = await self.client.post(
            f"{self.base_url}/api/auth/refresh",
            json={"refresh_token": self.refresh_token},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"token refresh failed: {resp.status_code} {resp.text[:120]}")
        pair = resp.json()
        self.token = pair["access_token"]
        self.refresh_token = pair["refresh_token"]
        self._token_ts = time.time()
        print("  [auth] token refreshed")

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        session_id: str | None = None,
        stream: bool = False,
    ) -> dict:
        """发一轮对话（自动重试 1 次；重试可能造成服务端重复落一轮消息，评测可接受）。

        Args:
            messages: OpenAI messages（runner 维护完整多轮历史）。
            session_id: 复用的会话 ID；None 则服务端新建。
            stream: True 走流式并采集首 token 时延。

        Returns:
            {"content", "session_id", "ttfb_ms", "e2e_ms", "prompt_tokens"}；
            非流式含上游 usage；流式 usage 缺省 0。

        Raises:
            RuntimeError: 两次尝试均失败，或流式探针未携带 session_id。
        """
        payload: dict = {
            "messages": messages,
            "stream": stream,
            "metadata": {"workspace_id": self.workspace_id},
        }
        if session_id:
            payload["metadata"]["session_id"] = session_id
        await self._ensure_fresh_token()  # 评测可能超过 access token 有效期
        # token 基线近似：本轮全量历史字符（1 字符≈1 token 上界）
        self.history_chars += sum(len(m["content"]) for m in messages)
        self.chat_calls += 1
        last_err = ""
        for _ in range(2):
            try:
                started = time.perf_counter()
                if not stream:
                    resp = await self.client.post(
                        f"{self.base_url}/v1/chat/completions",
                        headers={"Authorization": f"Bearer {self.token}"},
                        json=payload,
                    )
                    e2e = (time.perf_counter() - started) * 1000
                    resp.raise_for_status()
                    body = resp.json()
                    usage = body.get("usage") or {}
                    return {
                        "content": body["choices"][0]["message"]["content"],
                        "session_id": body["metadata"]["session_id"],
                        "ttfb_ms": 0.0,
                        "e2e_ms": e2e,
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                    }
                content, ttfb = await self._read_stream(payload)
                e2e = (time.perf_counter() - started) * 1000
                return {
                    "content": content,
                    "session_id": session_id or "",
                    "ttfb_ms": ttfb,
                    "e2e_ms": e2e,
                    "prompt_tokens": 0,
                }
            except (httpx.HTTPError, KeyError, ValueError, RuntimeError) as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(2)
        raise RuntimeError(f"chat failed twice: {last_err}")

    async def _read_stream(self, payload: dict) -> tuple[str, float]:
        """读 SSE 流：累计 delta 内容并测首 token 时延（session 由非流式轮开启，
        流式响应无 metadata 回传，调用方必须已传 session_id）。"""
        ttfb = 0.0
        chunks: list[str] = []
        async with self.client.stream(
            "POST",
            f"{self.base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.token}"},
            json=payload,
        ) as resp:
            resp.raise_for_status()
            started = time.perf_counter()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0].get("delta", {}).get("content")
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                if delta:
                    if not chunks:
                        ttfb = (time.perf_counter() - started) * 1000
                    chunks.append(delta)
        return "".join(chunks), ttfb

    async def preview(self, session_id: str, query: str) -> list[dict[str, str]]:
        """采集下一轮的注入上下文（不调 LLM、不落库；workspace_id 在 body）。"""
        await self._ensure_fresh_token()
        resp = await self.client.post(
            f"{self.base_url}/api/context/preview",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"workspace_id": self.workspace_id, "session_id": session_id, "query": query},
        )
        resp.raise_for_status()
        return resp.json()["messages"]

    async def create_task(self, title: str, priority: int) -> str:
        """REST 建任务（Task Continuity 的确定性起点；workspace_id 为 query 参数）。"""
        await self._ensure_fresh_token()
        resp = await self.client.post(
            f"{self.base_url}/api/tasks",
            headers={"Authorization": f"Bearer {self.token}"},
            params={"workspace_id": self.workspace_id},
            json={"title": title, "priority": priority},
        )
        resp.raise_for_status()
        return resp.json()["id"]

    async def usage_avg_tokens(self) -> float:
        """token_usages 聚合的每轮 prompt 均值（Token Cost 实际值；turns 在响应顶层）。"""
        await self._ensure_fresh_token()
        resp = await self.client.get(
            f"{self.base_url}/api/usage/summary",
            headers={"Authorization": f"Bearer {self.token}"},
            params={"workspace_id": self.workspace_id, "days": 7},
        )
        resp.raise_for_status()
        body = resp.json()
        turns = body.get("turns") or 0
        return body["totals"]["prompt_tokens"] / turns if turns else 0.0


# ---- 各套件执行 ----


@dataclass
class SuiteOutcome:
    """一个套件跑完的汇总（指标值 + judge 原始明细 + 错误列表）。"""

    name: str
    samples: int = 0
    errors: list[str] = field(default_factory=list)
    judge_raw: list[dict] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


async def _jctx(ec: EvalClient) -> JudgeContext:
    """从评测客户端构造 judge 上下文（先确保 token 新鲜，judge 复用同一身份）。"""
    await ec._ensure_fresh_token()
    return JudgeContext(base_url=ec.base_url, token=ec.token, workspace_id=ec.workspace_id)


async def run_long_turn(ec: EvalClient, groups: list[dict]) -> SuiteOutcome:
    """长对话集：逐组回放 → final probe 流式断言 + judge。

    产出：Long-turn Consistency、Context Precision（judge）、P95 ttfb/e2e 采样。
    """
    outcome = SuiteOutcome(name="long_turn")
    ttfb_samples: list[float] = []
    e2e_samples: list[float] = []
    passed = total = 0
    for g in groups:
        messages: list[dict[str, str]] = []
        session_id: str | None = None
        try:
            for turn in g["turns"]:
                messages.append({"role": "user", "content": turn["u"]})
                r = await ec.chat(messages, session_id=session_id)
                session_id = r["session_id"]
                messages.append({"role": "assistant", "content": r["content"]})
            # final probe：preview 采集注入 → 流式回答（测 ttfb）→ 规则断言 + judge
            probe = g["final_probe"]
            messages.append({"role": "user", "content": probe["u"]})
            injected = parse_injected_memories(await ec.preview(session_id, probe["u"]))
            r = await ec.chat(messages, session_id=session_id, stream=True)
            answer = r["content"]
            ok = all(any(kw in answer for kw in group) for group in probe["assert_contains"])
            passed += 1 if ok else 0
            total += 1
            ttfb_samples.append(r["ttfb_ms"])
            e2e_samples.append(r["e2e_ms"])
            outcome.judge_raw.append(
                await judge_context_precision(
                    await _jctx(ec), probe["u"], [i.content for i in injected], answer
                )
            )
            outcome.samples += 1
            status = "PASS" if ok else "FAIL"
            print(f"  [long_turn] {g['group_id']} assert={status} ttfb={r['ttfb_ms']:.0f}ms")
        except (RuntimeError, httpx.HTTPError) as exc:
            outcome.errors.append(f"{g['group_id']}: {exc}")
    outcome.extra = {
        "consistency": consistency(passed, total),
        "assert_detail": f"{passed}/{total}",
        "ttfb_ms": ttfb_samples,
        "e2e_ms": e2e_samples,
    }
    return outcome


async def run_memory_hit(ec: EvalClient, rows: list[dict]) -> SuiteOutcome:
    """记忆命中集：录入 → 干扰 → probe（条间并发 4）。

    产出：Memory Recall@5、Memory Precision、Hallucination（judge）。
    """
    outcome = SuiteOutcome(name="memory_hit")
    sem = asyncio.Semaphore(4)
    probes: list[tuple[list, list[str]]] = []  # (注入条目, 期望关键词组)
    trap_probes: list[tuple[list, list[str]]] = []
    judge_raw: list[dict] = []
    errors: list[str] = []
    samples = 0

    async def one(row: dict) -> None:
        nonlocal samples
        async with sem:
            try:
                r1 = await ec.chat([{"role": "user", "content": row["fact_turn"]}])
                session_id = r1["session_id"]
                messages = [
                    {"role": "user", "content": row["fact_turn"]},
                    {"role": "assistant", "content": r1["content"]},
                ]
                for i in range(row["gap"]):
                    d = _pick_distractor(row["fact_id"], i)
                    rd = await ec.chat(
                        messages + [{"role": "user", "content": d}], session_id=session_id
                    )
                    messages.extend(
                        [
                            {"role": "user", "content": d},
                            {"role": "assistant", "content": rd["content"]},
                        ]
                    )
                messages.append({"role": "user", "content": row["probe"]})
                injected = parse_injected_memories(await ec.preview(session_id, row["probe"]))
                rp = await ec.chat(messages, session_id=session_id)
                probes.append((injected, row["expected"]))
                trap_probes.append((injected, row.get("traps") or []))
                jctx = await _jctx(ec)
                judge_raw.append(await judge_hallucination(jctx, row["probe"], rp["content"]))
                samples += 1
                hit = any(all(kw in i.content for kw in row["expected"]) for i in injected[:5])
                print(f"  [memory_hit] {row['fact_id']} recall5={'PASS' if hit else 'FAIL'}")
            except (RuntimeError, httpx.HTTPError) as exc:
                errors.append(f"{row['fact_id']}: {exc}")

    await asyncio.gather(*(one(r) for r in rows))
    outcome.samples = samples
    outcome.errors = errors
    outcome.judge_raw = judge_raw
    outcome.extra = {
        "recall_at_5": recall_at_5(probes),
        "precision": memory_precision(trap_probes),
    }
    return outcome


async def run_conflict(ec: EvalClient, rows: list[dict]) -> SuiteOutcome:
    """冲突集：旧事实 → 干扰 → 新事实 → 干扰 → probe。

    产出：Fact Conflict Rate（注入并存）、回答正确率（辅助观察）。
    """
    outcome = SuiteOutcome(name="conflict")
    sem = asyncio.Semaphore(4)
    coexist_probes: list[tuple[list, str, str]] = []
    answer_ok = answer_total = 0
    errors: list[str] = []

    async def one(row: dict) -> None:
        nonlocal answer_ok, answer_total
        async with sem:
            try:
                r1 = await ec.chat([{"role": "user", "content": row["old_turn"]}])
                session_id = r1["session_id"]
                messages = [
                    {"role": "user", "content": row["old_turn"]},
                    {"role": "assistant", "content": r1["content"]},
                ]
                # 序列：干扰1 → 新事实 → 干扰2..N（gap 条，不同话术避免同句重复）
                seq = [_pick_distractor(row["conflict_id"], 0), row["new_turn"]]
                seq += [
                    _pick_distractor(row["conflict_id"], i + 1)
                    for i in range(max(0, row["gap"] - 1))
                ]
                for turn_content in seq:
                    rd = await ec.chat(
                        messages + [{"role": "user", "content": turn_content}],
                        session_id=session_id,
                    )
                    messages.extend(
                        [
                            {"role": "user", "content": turn_content},
                            {"role": "assistant", "content": rd["content"]},
                        ]
                    )
                messages.append({"role": "user", "content": row["probe"]})
                injected = parse_injected_memories(await ec.preview(session_id, row["probe"]))
                rp = await ec.chat(messages, session_id=session_id)
                coexist_probes.append((injected, row["old_kw"], row["new_kw"]))
                ok = any(kw in rp["content"] for kw in row["expected"])
                answer_ok += 1 if ok else 0
                answer_total += 1
                print(f"  [conflict] {row['conflict_id']} answer={'PASS' if ok else 'FAIL'}")
            except (RuntimeError, httpx.HTTPError) as exc:
                errors.append(f"{row['conflict_id']}: {exc}")

    await asyncio.gather(*(one(r) for r in rows))
    outcome.samples = answer_total
    outcome.errors = errors
    outcome.extra = {
        "conflict_rate": conflict_rate(coexist_probes),
        "answer_accuracy": consistency(answer_ok, answer_total),
    }
    return outcome


async def run_task_continuity(ec: EvalClient, rows: list[dict]) -> SuiteOutcome:
    """任务连续集：REST 建任务 → 新会话提问验证简报注入。

    产出：Task Continuity（断言关键词组命中比例）。
    """
    outcome = SuiteOutcome(name="task_continuity")
    passed = total = 0
    for row in rows:
        try:
            await ec.create_task(row["task_title"], row.get("priority", 0))
            # 新会话（不传 session_id）提问，验证跨会话任务简报注入
            r = await ec.chat([{"role": "user", "content": row["session_b_probe"]}])
            ok = all(any(kw in r["content"] for kw in grp) for grp in row["assert_contains"])
            passed += 1 if ok else 0
            total += 1
            outcome.samples += 1
            print(f"  [task] {row['task_id']} assert={'PASS' if ok else 'FAIL'}")
        except (RuntimeError, httpx.HTTPError) as exc:
            outcome.errors.append(f"{row['task_id']}: {exc}")
    outcome.extra = {
        "task_continuity": consistency(passed, total),
        "assert_detail": f"{passed}/{total}",
    }
    return outcome


# ---- 报告 ----


def build_report(
    label: str,
    outcomes: list[SuiteOutcome],
    token_actual: float,
    token_baseline: float,
    ttfb: list[float],
    e2e: list[float],
    judge_ctx: list[dict],
    judge_halu: list[dict],
) -> dict:
    """汇总 9 项指标为机器可读 dict（Markdown 渲染单独做）。

    Args:
        label: 本次评测标签（A/B 实验标识）。
        outcomes: 各套件结果。
        token_actual: token_usages 每轮 prompt 均值。
        token_baseline: 全量历史字符近似基线均值。
        ttfb/e2e: 首 token / 端到端采样（毫秒）。
        judge_ctx/judge_halu: 两类 judge 逐条输出。

    Returns:
        {"meta": {...}, "metrics": {...}, "auxiliary": {...}} 结构。
    """
    by = {o.name: o for o in outcomes}
    ctx_precision, ctx_n = aggregate_judge(judge_ctx, "contributed_ratio")
    halu_scores = [j for j in judge_halu if not j.get("error")]
    halu_rate = (
        sum(1 for j in halu_scores if j.get("has_unsupported")) / len(halu_scores)
        if halu_scores
        else 0.0
    )
    metrics = {
        "memory_recall_at_5": by["memory_hit"].extra["recall_at_5"] if "memory_hit" in by else 0.0,
        "memory_precision": by["memory_hit"].extra["precision"] if "memory_hit" in by else 0.0,
        "context_precision": ctx_precision,
        "long_turn_consistency": by["long_turn"].extra["consistency"] if "long_turn" in by else 0.0,
        "fact_conflict_rate": by["conflict"].extra["conflict_rate"] if "conflict" in by else 0.0,
        "hallucination_rate": halu_rate,
        "token_savings": token_savings(token_actual, token_baseline),
        "p95_ttfb_ms": percentile(ttfb, 95),
        "p95_e2e_ms": percentile(e2e, 95),
        "task_continuity": by["task_continuity"].extra["task_continuity"]
        if "task_continuity" in by
        else 0.0,
    }
    return {
        "meta": {
            "label": label,
            "generated_at": datetime.now(UTC).isoformat(),
            "samples": {o.name: o.samples for o in outcomes},
            "errors": {o.name: o.errors for o in outcomes},
            "judge_effective": {"context_precision": ctx_n, "hallucination": len(halu_scores)},
            "token_note": f"actual_avg={token_actual:.0f} baseline_avg≈{token_baseline:.0f}"
            "（全量历史字符上界近似；绝对值仅供参考，以 A/B 相对变化为主）",
        },
        "metrics": metrics,
        "auxiliary": {
            "conflict_answer_accuracy": by["conflict"].extra.get("answer_accuracy")
            if "conflict" in by
            else None,
            "long_turn_assert": by["long_turn"].extra.get("assert_detail")
            if "long_turn" in by
            else None,
            "task_assert": by["task_continuity"].extra.get("assert_detail")
            if "task_continuity" in by
            else None,
        },
    }


def render_markdown(report: dict, baseline: dict | None) -> str:
    """渲染 Markdown 报告（含 A/B baseline 对比列，若有）。"""
    header = "| 指标 | 本次值 | 目标 | 达标 |" + (" 基线值 | Δ |" if baseline else "")
    divider = "|---|---|---|---|" + ("---|---|" if baseline else "")
    lines = [
        f"# EchoDesk 评测报告 · {report['meta']['label']}",
        "",
        f"生成时间：{report['meta']['generated_at']}　样本：{report['meta']['samples']}",
        "",
        header,
        divider,
    ]
    for key, (target, label_text) in TARGETS.items():
        value = report["metrics"][key]
        higher_better = key not in (
            "fact_conflict_rate",
            "hallucination_rate",
            "p95_ttfb_ms",
            "p95_e2e_ms",
        )
        ok = value >= (target or 0) if higher_better else value <= (target or 0)
        cell = f"{value:.4f}" if key not in ("p95_ttfb_ms", "p95_e2e_ms") else f"{value:.0f}ms"
        row = f"| {key} | {cell} | {label_text} | {'✅' if ok else '❌'} |"
        if baseline:
            bval = baseline["metrics"][key]
            bcell = f"{bval:.4f}" if key not in ("p95_ttfb_ms", "p95_e2e_ms") else f"{bval:.0f}ms"
            row += f" {bcell} | {value - bval:+.4f} |"
        lines.append(row)
    err_counts = {k: len(v) for k, v in report["meta"]["errors"].items() if v}
    lines += [
        "",
        "## 辅助观察",
        "",
        f"- 冲突组回答正确率：{report['auxiliary']['conflict_answer_accuracy']}",
        f"- 长对话断言明细：{report['auxiliary']['long_turn_assert']}",
        f"- 任务断言明细：{report['auxiliary']['task_assert']}",
        f"- token 口径：{report['meta']['token_note']}",
        f"- judge 有效样本：{report['meta']['judge_effective']}",
        f"- 错误样本：{err_counts or '无'}",
        "",
        "> 指标定义与口径见 docs/01 §8 与 evals/README.md；数字为真实链路实测。",
        "",
    ]
    return "\n".join(lines)


# ---- 主流程 ----


async def main() -> int:
    """入口：解析参数 → 建评测账号 → 跑套件 → 出报告。"""
    parser = argparse.ArgumentParser(description="EchoDesk M-13 评测 runner")
    parser.add_argument("--label", required=True, help="本次评测标签（A/B 实验标识）")
    parser.add_argument(
        "--suite",
        default="all",
        choices=["all", "long_turn", "memory_hit", "conflict", "task_continuity"],
    )
    parser.add_argument("--base-url", default="http://localhost:8100")
    parser.add_argument("--username", default="evalbot")
    parser.add_argument("--password", default="evalbot-2026")
    parser.add_argument("--baseline", default=None, help="旧报告 JSON 路径（生成对比列）")
    args = parser.parse_args()

    suites = list(_DATASET_REQUIRED) if args.suite == "all" else [args.suite]
    ec = await EvalClient.create(args.base_url, args.username, args.password, args.label)
    print(f"label={args.label} suites={suites}")

    outcomes: list[SuiteOutcome] = []
    for suite in suites:
        rows = load_jsonl(suite)
        print(f"== suite {suite}: {len(rows)} rows ==")
        if suite == "long_turn":
            outcomes.append(await run_long_turn(ec, rows))
        elif suite == "memory_hit":
            outcomes.append(await run_memory_hit(ec, rows))
        elif suite == "conflict":
            outcomes.append(await run_conflict(ec, rows))
        else:
            outcomes.append(await run_task_continuity(ec, rows))

    # 时延采样与 judge 明细来自对应套件（单套件模式下其余为空列表，指标记 0）
    ttfb = [s for o in outcomes if o.name == "long_turn" for s in o.extra.get("ttfb_ms", [])]
    e2e = [s for o in outcomes if o.name == "long_turn" for s in o.extra.get("e2e_ms", [])]
    judge_ctx = [j for o in outcomes if o.name == "long_turn" for j in o.judge_raw]
    judge_halu = [j for o in outcomes if o.name == "memory_hit" for j in o.judge_raw]

    token_actual = await ec.usage_avg_tokens()
    token_baseline = ec.history_chars / max(1, ec.chat_calls)
    report = build_report(
        args.label, outcomes, token_actual, token_baseline, ttfb, e2e, judge_ctx, judge_halu
    )

    baseline = None
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))

    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    json_path = REPORTS_DIR / f"eval_{args.label}_{stamp}.json"
    md_path = REPORTS_DIR / f"eval_{args.label}_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report, baseline), encoding="utf-8")
    print(f"\nreport: {md_path}")
    for key, value in report["metrics"].items():
        print(f"  {key:26s} {value}")
    await ec.client.aclose()
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # PS5.1 中文输出
    raise SystemExit(asyncio.run(main()))
