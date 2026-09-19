"""观测子系统（M-12）：Langfuse 追踪薄封装与 LLM 价目表。

模块：
  - tracing：span()/turn()/generation() 上下文管理器（未配置时 no-op 降级）
  - pricing：config/pricing.yaml 价目加载与 cost_usd 折算（M-11 遗留恒 0 的补齐）
"""
