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
| Memory Recall@5 | memory_hit | 期望关键词组被注入的 **semantic/procedural 桶各自 top-5** 中**同一条目全量覆盖**（判定窗口双桶前 5，见「Memory Recall@5 口径」） | ≥80% |
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

## 异步写路径的两阶段回放（Fix 轮引入）

记忆抽取是后台异步链路（LLM 抽取/仲裁 + 同用户串行队列，M-04 Fix D），
`probe` 采集注入前**必须等事实落库**，否则指标混入队列延迟噪声——
Fix 轮实测：抽取产出正确但入队执行晚于 probe 数分钟，Recall/Conflict
被误判 FAIL（事实"丢库"假象）。

等待采用**两阶段回放**（memory_hit / conflict 套件；long_turn 于 fix_v8_lt
轮加入——每组 30 轮回放后、final probe 前，以该组全部 `turn["fact_keys"]`
复用同一等待函数）：

1. 阶段1：回放全部场景（录入→干扰，或 旧事实→干扰→新事实→干扰），
   不等待、不采集
2. 阶段2：批量轮询 `GET /api/memories?q=<关键词>`（并发 8、间隔 5s、
   总超时 `--wait-timeout` 默认 1200s）直至全部事实可见
3. 阶段3：逐场景 preview 采集注入 + probe 回答 + judge

为何不逐行等待：串行队列下后面场景的抽取排在队列深处（memory_hit
20 行约 140 轮 chat，尾部积压可达 20 分钟），逐行 120s 必然超时；
先全部回放再统一等待，等待期间无新 chat 负载，队列持续消化，收敛
时间可控。超时未可见的照常 probe、由指标如实反映缺库（报告逐行明细
中 `wait_s=-1` 标注）。

扩充数据集时，新事实话术必须包含可检索的字面关键词（new_kw/expected
与 content 字面匹配）。

## Memory Recall@5 口径（四期修正）

判定窗口为 **semantic 与 procedural 两桶各自的前 5 条**（桶内保持检索
score 排序），两桶任一条目全量覆盖期望关键词组即命中。口径演进：

- **一期→三期（渲染序 → semantic 单桶）**：fix_v9_mh 取证实锤——builder
  装配渲染 procedural 先于 semantic，跨场景全局条目（日程/计划/偏好）
  在几乎每个 probe 的渲染序列前排占 3-4 席，按渲染顺序切 `[:5]` 会把
  判定窗口占满——6 行 FAIL 的目标 semantic 条目检索 sim 实测全部第 1，
  却渲染在第 6-8 位（假 FAIL），故收窄为 semantic 单桶。
- **四期（fix_v13，semantic 单桶 → 双桶前 5）**：fix_v12_mh 65 FAIL
  分桶取证 + 三口径重放（semantic 单桶 33 / 注入区 sem+proc 59 / 全量
  selected 59）翻案主矛盾——26/65 FAIL 行的目标条目**实际已被注入
  procedural 桶**（抽取器把时间/地点/数量/偏好类事实标为 procedural，
  如「美团周四面试」「杭州求职范围」），用户提问时 LLM 本可答对，
  semantic 单桶判定把「type 标错但已注入」系统性漏判。episodic/task
  不入窗：期望事实若只存在于会话摘要/任务条目，说明独立记忆抽取失败，
  判 FAIL 是正确信号。type 标注错乱本身是抽取层问题（后续专项治理），
  不应由检索评测买单。

已知的真实产品缺陷（fix_v10_mh 取证确认为 hit 马太，检索权重已修；
fix_v13 补检索分区保底 `_balanced_select` + candidate_k 20→50）：
高频"万金油"条目 hit_count 跨查询累积、20 次即饱和满分，恒压高 sim 冷
条目——检索权重 sim 0.6 / hit 0.05 钳制（见 retriever.py 权重注释）。

## Fact Conflict Rate 口径（Fix 轮修正）

并存判定限定为**两条独立条目**：存在条目 A 含 `old_kw` 且不含
`new_kw`，且存在条目 B 含 `new_kw` 且不含 `old_kw`。

单条目内新旧同现**不算并存**：supersede/MERGE 后的新条目以背景口吻
提及旧值（"旧号 138… 已注销""覆盖此前深圳偏好"）是冲突消解的正确
产物。Fix 轮实测教训：旧口径（关键词分别出现即并存）曾把 6 行 probe
中 4 行误判并存（rate 0.667），而库内实况 8 组冲突全部 supersede 成功、
零 CONFLICTED 条目——纯口径误伤，与检索/仲裁质量无关。

## 全量串联口径的队列积压伪影（fix_v4 轮发现）

`--suite all` 单账号串联 4 套件（34 行 ≈ 250+ 轮 chat）时，抽取串行
队列深度超过 1200s 等待窗口：fix_v4 实测 15/34 行 keyword 等待超时，
Recall@5 被拉低到 0.35（对比单套件口径 fix_v3_mh 的 0.636）。

库内取证结论（按分钟统计该账号 memories.created_at）：

- **是延迟、不是丢失**：评测报告生成后队列仍在消化，尾部约 7 分钟后
  消化完毕，全程 358 条记忆零丢失
- **消化速率量化**：per-user 串行抽取约 2~5 条/分钟（单条耗时以 LLM
  抽取调用为主；long_turn 超长轮次显著拉长单任务耗时）
- **修复验证同轮通过**：错误样本 0（Fix E 后无死锁 502/500，业务日志
  零 ERROR/WARNING，`record_hits_degraded` 降级未触发）；Fact Conflict
  Rate 0.0（vs 基线 0.625）；Precision 0.989 / Context Precision 0.938
  均为历史最佳

**口径约定**：依赖异步落库的套件（memory_hit / conflict / long_turn——
fix_v7_lt 实锤 long_turn 的 30 轮连发同样触发队列积压：`learning.kafka`
probe 结束 5 分钟后才落库，0/2 系 probe 抢跑伪影）评测时必须独立账号
**分套件跑**（即 fix_v3 / fix_v3_mh 的方式，套件级 A/B 读数以那两轮
为准）；`--suite all` 适合 task_continuity 这类同步落库套件，或作为
容量观测（队列深度/消化速率）压测口径。
