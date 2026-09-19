# EchoDesk 评测体系（M-13）

回放式评测：向**真实部署的 memory-service** 回放对话数据集，采集注入上下文与回答，
计算 docs/01 §8.2 定义的 9 项指标，输出 Markdown + JSON 双格式报告。
不做 mock——指标反映端到端真实链路（含 LLM 抽取、检索、装配、压缩）。

## 目录结构

```
evals/
  datasets/                  # 回放数据集（JSONL，可手工扩充）
    long_turn.jsonl          # 长对话（30 轮×N 组）：Consistency / ttfb / judge
    memory_hit.jsonl         # 事实命中（录入→干扰→探针）：Recall@5 / Precision / Hallucination
    conflict.jsonl           # 事实冲突（旧→新→探针）：Conflict Rate
    task_continuity.jsonl    # 任务跨会话延续：Task Continuity
  metrics.py                 # 规则可计算的指标纯函数（无外部依赖，可独立单测）
  judges.py                  # LLM-as-judge（走 /v1/tasks/completions，不落库）
  run_eval.py                # 主引擎：注册评测账号 → 回放 → 采集 → 报告
  reports/                   # 历次评测报告（md + json，入库留存）
```

## 快速开始

前置：memory-service 已在 `http://localhost:8100` 运行（LLM/embedding 依赖可用）。

```bash
cd services/memory-service
python evals/run_eval.py --label default --suite all          # 全量（约 280 次对话，8~12 分钟）
python evals/run_eval.py --label smoke --suite conflict       # 单套件
```

A/B 对比（先跑一轮作为基线，再切换配置重跑）：

```bash
# 第二轮：换 context_budget_profile=memory_first 重启服务后
python evals/run_eval.py --label memory_first --suite all \
  --baseline evals/reports/eval_default_<stamp>.json
```

报告输出至 `evals/reports/eval_<label>_<YYYYMMDD-HHMM>.md/.json`。

## 指标口径（与 docs/01 §8.2 对齐）

| 指标 | 来源套件 | 口径 | 目标 |
|---|---|---|---|
| Memory Recall@5 | memory_hit | 期望关键词组被注入条目 top-5 中**同一条目全量覆盖** | ≥80% |
| Memory Precision | memory_hit | 注入条目中含陷阱关键词组的占比（越低越好，报 1-占比） | ≥70% |
| Context Precision | long_turn | judge 判定注入记忆对回答"有贡献"的条目占比 | ≥70% |
| Long-turn Consistency | long_turn | final probe 回答的关键词断言通过率（组内任一命中） | ≥90% |
| Fact Conflict Rate | conflict | 同一探针注入中**新旧事实并存**的比例 | ≤10% |
| Hallucination Rate | memory_hit | judge 判定回答含无依据关键论断的比例 | ≤5% |
| Token Cost / Turn | 全部 | 1 − 实际每轮 prompt 均值 / 全量历史基线均值 | ≥40% |
| P95 Latency | long_turn | final probe 流式首 token / 端到端的 95 分位 | ≤2s / ≤8s |
| Task Continuity | task_continuity | 建任务后新会话回答的关键词断言通过率 | ≥80% |

### 注入条目的解析依据

`/api/context/preview` 返回的 messages 中，builder 以固定格式渲染记忆条目：

```
<memory type="procedural|semantic|..." source="extract|task:<id>|...">内容</memory>
```

`metrics.parse_injected_memories` 用正则精确解析（type/source/content 三元组），
Recall/Precision/Conflict 均基于该结构判定，不依赖回答文本猜测。

### token 基线的近似性

实际值来自 `token_usages` 聚合（含 system prompt + 记忆 + RAG 注入）；
基线为 runner 维护的全量对话历史按"1 字符≈1 token 上界"估算。两值同为
全部轮次混合口径，**绝对值仅供参考，A/B 相对变化（--baseline 对比列）是主要读数**。

## 数据集格式（手工扩充指南）

### long_turn.jsonl

```json
{"group_id": "lt-01", "scenario": "场景描述",
 "turns": [{"u": "用户话术", "fact_keys": ["可留空"]}, ...],
 "final_probe": {"u": "综合探针问题",
   "assert_contains": [["关键词组1"], ["关键词组2", "备选"]],
   "note": "断言意图说明"}}
```

- `assert_contains` 每组内任一关键词命中即该组通过（允许同义表述）
- 组内轮次建议 ≥20 轮，覆盖"背景→偏好→弱项→计划变化"的信息梯度

### memory_hit.jsonl

```json
{"fact_id": "mh-01", "fact_turn": "录入事实的话术",
 "gap": 4, "probe": "探针问题",
 "expected": ["须同时命中同一条目的关键词"],
 "traps": [["不应注入的陷阱关键词组"]]}
```

- `gap` 为 3-6 条干扰轮（runner 自动插入无关闲聊）
- `traps` 是"从未说过的事实"，用于量化抽取偏多/误注入

### conflict.jsonl

```json
{"conflict_id": "cf-01", "old_turn": "旧事实话术", "new_turn": "新事实话术",
 "gap": 4, "probe": "探针", "old_kw": "旧关键词", "new_kw": "新关键词",
 "expected": ["回答应含的关键词（任一命中）"]}
```

### task_continuity.jsonl

```json
{"task_id": "tc-01", "task_title": "断言关键词全部写进标题",
 "priority": 1, "session_b_probe": "新会话探针",
 "assert_contains": [["关键词组", "备选"]]}
```

注意：任务简报注入只含 title（TaskCreate 无 description），断言关键词必须全部体现在标题里。

## 隔离与清理

- 每次运行注册唯一账号 `evalbot-<label>-<时间戳>`（自带独立 workspace），
  不污染真实用户数据；评测数据保留用于排查，可事后在面板删除
- judge 走无状态任务端点，不进会话历史
- 评测流量自动进 Langfuse（trace 可审计 judge 与回放行为）

## 已知限制

- 回放是"快进"对话（间隔数秒而非真实用户节奏），episodic/滚动摘要的时间衰减
  维度未被覆盖
- 单套件模式下其余指标记 0（报告注明样本量）
- 数据集当前为种子规模（2+20+8+4 条），格式支持扩到设计全量
