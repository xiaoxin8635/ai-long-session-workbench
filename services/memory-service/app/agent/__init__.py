"""Agent 图模块（M-08 M3，docs/06 §10）：LangGraph 编排的对话运行时。

流程：load_context（记忆/RAG 装配）→ generate（流式生成 + 工具协议）
→ tools（分级执行，external 经 interrupt 挂起等确认）→ postprocess（落库）。
"""
