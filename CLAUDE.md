# CLAUDE.md — AI 长会话知识工作台 · 项目级 AI 开发规范

> 本文件是 Claude Code 在本仓库工作时的强制规范。每次会话自动加载，与用户全局规范叠加生效。

## 语言与沟通

- 对话与文档使用中文；代码标识符、技术术语、commit 类型保留英文
- 代码注释使用中文，术语保留英文

## 文件操作（必须遵守）

- 所有文件操作使用带盘符和反斜杠的完整 Windows 绝对路径（如 `D:\ai_play\ai_long_project\...`）
- 项目根目录：`D:\ai_play\ai_long_project`

## 项目背景

- 本项目是"AI 长会话知识工作台"：Open WebUI 做产品壳，自研 `memory-service`（FastAPI + LangGraph）承载四层记忆、上下文组装、RAG、MCP 工具、观测
- 完整设计见 `docs\01-项目开发内容与实现方案.md`，开发任何模块前必须先读对应章节
- 本项目基于 Open WebUI 二次开发：涉及 `open-webui/` 目录时，保留上游 license、版权声明与署名，不得删除或遮挡

## 技术栈（不要引入清单之外的重量级依赖）

- Python 3.11+ / FastAPI / Pydantic v2 / SQLAlchemy 2.x + Alembic
- LangGraph / MCP / PostgreSQL / Redis / Milvus(或 pgvector) / Langfuse
- 测试：pytest + httpx；代码质量：ruff（lint + format）
- 部署：Docker Compose

## 目录约定

```
open-webui/                 # 上游产品壳（二开区域，注意 license）
services/memory-service/    # ★ 自研核心服务（主要开发区）
  app/api/                  # 路由层：只做参数校验和编排，不放业务逻辑
  app/models/               # SQLAlchemy ORM
  app/schemas/              # Pydantic 模型
  app/memory/               # 四层记忆与抽取/检索/冲突管线
  app/context/              # 上下文组装与 token 预算
  app/rag/  app/agent/  app/tools/  app/observability/
  alembic/  workers/  tests/  evals/
deploy/                     # docker-compose 与环境编排
docs/                       # 所有技术文档
config/                     # 预算策略等 YAML 配置
```

## 代码规范（摘要，完整版见 docs\02-开发规范.md）

1. **注释**：每个新文件必须有文件级 docstring；每个类必须有类级 docstring；每个函数/方法必须有函数级 docstring（含 Args / Returns / Raises）
2. **类型标注**：所有函数签名必须完整类型标注；ORM 与 API 边界用 Pydantic 模型，禁止裸 dict 传递业务数据
3. **命名**：Python 用 snake_case，类用 PascalCase，常量用 UPPER_SNAKE_CASE；数据库表/字段用 snake_case 复数表名
4. **禁止**：虚假实现（TODO 糊弄、未实现的空函数）、被注释掉的死代码、print 调试语句入库（用 logging）
5. **错误处理**：API 统一 RFC 7807 problem+json 错误结构；对外部依赖（LLM/DB/Redis）必须有超时与异常降级

## 数据库变更流程

- 改 ORM 模型后必须生成 Alembic 迁移：`alembic revision --autogenerate -m "<描述>"`，人工检查迁移脚本后才能应用
- 禁止手改已提交的迁移文件

## 改动后的必做清单

1. `ruff check` + `ruff format` 通过
2. 相关 pytest 通过；新增功能必须附带测试
3. 同步更新受影响的文档（docs/ 下对应章节、API 文档、注释）
4. 一旦发现 Bug 立即修复，并按蟑螂法则排查同类问题

## Git 规范

- Conventional Commits：`feat|fix|docs|chore|refactor|test|perf: 中文描述`
- 分支：`main`（可发布）/ `feature/<模块名>` / `fix/<问题名>`
- 提交前确认 `.env`、密钥、`data/`、`volumes/` 未入库（.gitignore 已配置，仍需自查）
- 不使用 `git push --force`；不跳过 hooks

## 安全红线

- 密钥（LLM API key、DB 密码、JWT secret）只放 `.env`（已被 .gitignore 排除），配置模板写 `.env.example`
- 任何用户输入必须经 Pydantic 校验；SQL 一律走 ORM / 参数化，禁止字符串拼接
- 涉及工具调用权限（risk_level）的逻辑改动必须补审计日志测试

## 进程与端口管理

- 启动/重启任何服务前，先用 `netstat -ano | findstr <port>` 检查端口占用，被占用则先 `taskkill //f //pid <pid>` 再启动
- 端口约定：Open WebUI 3000 / memory-service 8100 / PostgreSQL 5432 / Redis 6379 / Milvus 19530 / Langfuse 3001

## 工作方式

- 涉及任务规划、方案选择、执行有风险命令时，先用 MCP interactive_feedback 与用户确认
- 需要当前时间时调用 MCP 时间工具
- 每完成一个模块，汇报内容：做了什么、验证结果（测试输出）、下一步建议
