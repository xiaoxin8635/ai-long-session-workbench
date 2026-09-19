"""工具子系统（M-09，docs/01 §5.6 / docs/06 §11）。

分层：registry（注册表：名称/schema/risk_level/handler）→ builtin（首批
内置工具：todo / doc_reader / web_fetch）→ router（执行器：分级权限、
external 确认流、审计、降级）。MCP 外接协议客户端（stdio/HTTP）留 M3，
registry 的 ToolDefinition 与 MCP tool 定义同构，届时直接适配。
"""
