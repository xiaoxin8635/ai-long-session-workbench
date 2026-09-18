"""Context Builder 包（M-05）：token 预算内的上下文统一组装。

模块划分（docs/06 §7）：
  - tokenizer：token 计数（全服务统一口径）
  - schemas：ContextItem / ContextCandidates / AssembledContext 等装配数据结构
  - budget：预算 profile 加载（config/budgets/*.yaml，占比 × 窗口换算）
  - builder：ContextBuilder（候选裁剪 + 渲染 + 高层取数编排）
"""
