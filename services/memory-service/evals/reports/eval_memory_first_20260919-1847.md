# EchoDesk 评测报告 · memory_first

生成时间：2026-09-19T10:47:20.788098+00:00　样本：{'long_turn': 2, 'memory_hit': 18, 'conflict': 8, 'task_continuity': 4}

| 指标 | 本次值 | 目标 | 达标 | 基线值 | Δ |
|---|---|---|---|---|---|
| memory_recall_at_5 | 0.4444 | ≥80% | ❌ | 0.3684 | +0.0760 |
| memory_precision | 0.9931 | ≥70% | ✅ | 0.9655 | +0.0275 |
| context_precision | 0.9375 | ≥70%（judge） | ✅ | 0.8660 | +0.0715 |
| long_turn_consistency | 0.5000 | ≥90% | ❌ | 0.5000 | +0.0000 |
| fact_conflict_rate | 0.7500 | ≤10% | ❌ | 0.6250 | +0.1250 |
| hallucination_rate | 1.0000 | ≤5%（judge） | ❌ | 1.0000 | +0.0000 |
| token_savings | 0.7513 | ≥40% | ✅ | 0.7910 | -0.0396 |
| p95_ttfb_ms | 1104ms | 首 token ≤2000ms | ✅ | 1553ms | -449.3176 |
| p95_e2e_ms | 71330ms | 端到端 ≤8000ms | ❌ | 43964ms | +27366.0434 |
| task_continuity | 1.0000 | ≥80% | ✅ | 0.7500 | +0.2500 |

## 辅助观察

- 冲突组回答正确率：0.75
- 长对话断言明细：1/2
- 任务断言明细：4/4
- token 口径：actual_avg=1022 baseline_avg≈4110（全量历史字符上界近似；绝对值仅供参考，以 A/B 相对变化为主）
- judge 有效样本：{'context_precision': 2, 'hallucination': 18}
- 错误样本：{'memory_hit': 2}

> 指标定义与口径见 docs/01 §8 与 evals/README.md；数字为真实链路实测。
