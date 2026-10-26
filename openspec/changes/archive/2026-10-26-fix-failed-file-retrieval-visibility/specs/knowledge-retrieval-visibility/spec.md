# Spec Delta

## Purpose

规定知识文件生命周期状态与双路检索可见性之间的映射：只有尚未终结的文件才可被引擎召回，一旦进入失败或已替代终态，其切片 MUST 从向量与词法两路同时下线，避免用户看到「解析失败却仍被引用」的自相矛盾结果。

## ADDED Requirements

### Requirement: terminal-states-excluded-from-retrieval
双路检索 MUST 以所属文件的生命周期状态为可见性判据：`failed` 与 `superseded` 文件的切片 MUST 不出现在任何召回结果与 chat 的 citations 中；`parsing` 与 `embedded` 文件的切片 MUST 保持可召回。

#### Scenario: failed-file-not-returned-by-lexical-path
- GIVEN `guide.md` 摄取失败，status 为 `failed` 且持有 60 条非空 tokens 切片
- WHEN 用户以该文档正文中的关键词发起对话
- THEN 检索结果的 hits 不含该文件的任何切片
- AND finish 片的 `metadata.citations` 不含该文件的引用

#### Scenario: failed-file-not-returned-by-vector-path
- GIVEN `guide.md` status 为 `failed`，但部分切片已回填 embedding
- WHEN 执行一次带向量的混合检索
- THEN 向量路候选不含该文件已回填的切片
- AND 响应码为 200 而非报错

#### Scenario: parsing-file-remains-retrievable
- GIVEN `new.md` 刚上传完成，status 为 `parsing` 且切片 tokens 已就绪
- WHEN 用户在其摄取完成前检索该文档关键词
- THEN 该文件的切片仍可被词法路召回
- AND 其行为与变更前一致（摄取进行中不被视为下线）

### Requirement: failed-transition-clears-retrieval-keys
摄取任务最终转 `failed` 时，系统 MUST 在处理该迁移的同一次提交内把该文件全部切片的 `embedding` 置 NULL、`tokens` 置空数组，与 `superseded` 的下线方式保持一致。

#### Scenario: mark-failed-clears-tokens-and-embeddings
- GIVEN `guide.md` status 为 `parsing` 且切片已有 tokens 与部分 embedding
- WHEN 摄取重试耗尽后转入失败分支
- THEN 文件 status 变为 `failed`
- AND 其全部切片的 tokens 为空数组且 embedding 为 NULL

#### Scenario: failure-writes-are-atomic
- GIVEN 数据库在清理切片键时抛出异常
- WHEN 失败迁移执行
- THEN 状态改写与切片清理 MUST 在同一事务内提交
- AND 异常时记录 `knowledge_mark_failed_error` 日志，不得留下「tokens 未清却已标 failed」的半成品状态

### Requirement: failure-state-remains-diagnosable
文件进入 `failed` 后，其列表项与切片明细 MUST 仍可读，使用户能看到失败状态与错误原因以便决定是否重传。

#### Scenario: failed-file-still-listed-with-reason
- GIVEN `guide.md` 已因 embedding 服务不可用而失败
- WHEN 用户打开知识库文件列表
- THEN 列表仍包含该文件且 status 显示为 `failed`
- AND 可查看到对应的失败原因以便决定是否重传

#### Scenario: reupload-of-failed-content-redispatches-ingest
- GIVEN `guide.md` status 为 `failed`
- WHEN 用户上传字节完全相同的同名文件
- THEN 返回既有文件记录（checksum 幂等）
- AND 系统重新派发摄取任务，成功后该文件恢复 `embedded` 并重新可召回
