# Tasks

## 1. 检索层可见性过滤

- [x] 1.1 在 `services/memory-service/app/repositories/knowledge_repo.py` 的 `vector_search` 中 join `knowledge_files` 并排除 `failed`/`superseded` 状态，验证其 select 语句仍只返回同 workspace 切片且语法可被 SQLAlchemy 编译
- [x] 1.2 在同文件 `load_corpus_tokens` 中施加同一 join 与排除条件（保留 `tokens` 非空过滤），验证 BM25 语料路径下 corpus 不再含 failed 文件切片
- [x] 1.3 修订本模块头 docstring 中「免 join 设计 / 无需回查 knowledge_files.status」的过期说明，改为以文件状态为判据，验证表述与实现一致

## 2. 失败迁移的状态对称化

- [x] 2.1 在 `services/memory-service/app/rag/ingest.py` 的 `_mark_failed` 中，在同一会话同一 commit 内对目标文件切片执行 `embedding=None, tokens=[]` 双清，验证失败后切片 tokens 为空数组、embedding 为 NULL
- [x] 2.2 补充回归测试：构造 embedding 调用失败导致摄取转 failed 的场景，断言 `_mark_failed` 后 `load_corpus_tokens` 不再返回其切片，验证该用例通过

## 3. 集成验证

- [x] 3.1 在 `services/memory-service/tests/test_rag_pipeline.py` 新增 failed 文件双路不可召回用例（对齐 `test_retriever_bm25_only_and_superseded_excluded` 的构造手法），验证新用例通过且原有用例无回归
- [x] 3.2 跑全量后端测试套件，验证 210+ 用例全绿且 ruff check / format 零告警
- [x] 3.3 重启 memory-service 并对线上已 failed 的 `面试高频问题手册.md` 做实机复验，验证检索其关键词零命中且对话 citations 不再含该文件，同时 `embedded` 文件召回不受影响

## Workflow follow-up

- 待用户确认后归档本变更（openspec archive fix-failed-file-retrieval-visibility）。
