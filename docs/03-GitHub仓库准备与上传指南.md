# GitHub 仓库准备与上传指南

> 目标：把本地仓库 `D:\ai_play\ai_long_project` 安全地发布到 GitHub，作为个人项目的代码托管与展示门面。
>
> 文档版本：v1.0（2026-09-18）

---

## 1. 上传前安全检查（必做）

### 1.1 密钥扫描

```powershell
# 确认 .env 等敏感文件不在跟踪列表中（应无输出）
git ls-files | Select-String -Pattern "\.env$|\.env\..*key|secret|credential"
# 只允许 .env.example 存在
git ls-files | Select-String -Pattern "\.env\.example"
```

历史上已配置的忽略规则见根目录 `.gitignore`。若曾误提交密钥：吊销密钥 → `git filter-repo` 清历史 → 重新初始化远端。

### 1.2 个人数据检查

- `docs/` 中的示例对话、评测集不得包含真实姓名/手机号/身份证等求职敏感信息（脱敏后入库）
- Demo 截图与录屏单独脱敏审查后再放 `docs/assets/`

## 2. 创建 GitHub 仓库

建议配置：

| 配置项 | 建议值 | 理由 |
|---|---|---|
| 可见性 | **Private** 起步 | 个人项目含真实使用数据；准备投简历时再转 Public 展示 |
| 名称 | `ai-long-session-workbench` | 英文命名，与简历描述一致 |
| 描述 | AI 长会话知识工作台：多会话 Agent 系统（长期记忆 / 上下文工程 / RAG / MCP） | 搜索友好 |
| 初始化 | 不勾选 README/.gitignore/license | 本地已有，避免冲突 |

创建入口：`https://github.com/new`

## 3. 首次推送步骤

```powershell
# 1. 本地已是 git 仓库（main 分支），添加远端
git remote add origin https://github.com/<你的用户名>/ai-long-session-workbench.git

# 2. 推送
git push -u origin main

# 3. 验证
git remote -v
```

若使用 SSH：`git remote set-url origin git@github.com:<你的用户名>/ai-long-session-workbench.git`

## 4. 后续分支保护（转 Public 前配置）

1. Settings → Branches → Add rule → `main`
2. 规则：Require a pull request before merging（个人项目可豁免 approvals 数）
3. 勾选 Require status checks（接入 CI 后再启用，见第 6 节）

## 5. CI 建议（M2 后接入）

`.github/workflows/ci.yml`：push / PR 触发 → `uv sync` → `ruff check` → `pytest -q`（单测部分，不依赖 docker 的用例）。集成测试留在本地 compose 环境。

## 6. License 注意事项（重要）

1. **自研部分**（`services/memory-service/`、`evals/`、`docs/`）：拟 MIT，添加 `LICENSE` 文件时注明范围
2. **Open WebUI 二开部分**：引入其代码（fork / 子目录 / patch）前，复查其当前 license（含品牌与附加条款）：
   - 保留其 LICENSE 文件、版权声明、界面 "Powered by Open WebUI" 署名
   - 不将上游代码改头换面声称为自研
   - README 模块来源表（已建）持续如实维护
3. 引入方式建议：优先 `open-webui/` 作为独立目录 vendored 或 git submodule，其 license 文件随目录保留，边界清晰

## 7. 仓库门面维护清单

| 项目 | 位置 | 状态 |
|---|---|---|
| README（定位/架构/模块来源表） | 根目录 | ✅ 已建立，随里程碑更新 |
| 架构图 | README + docs/01 | ✅ 已建立 |
| 开发规范 | docs/02 | ✅ 已建立 |
| 快速开始 | README | ⏳ M1 完成后补 Docker Compose 步骤 |
| Demo 录屏 / 截图 | docs/assets | ⏳ M3 产出 |
| 评测报告 | evals/reports | ⏳ Day 13 产出（真实数据） |
