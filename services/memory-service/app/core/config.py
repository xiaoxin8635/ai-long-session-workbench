"""服务配置（docs/06 §M-01）。

统一从环境变量（前缀 MEMORY_SERVICE_）与 .env 文件加载，
全服务唯一的配置入口；业务代码禁止直接调用 os.getenv。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """memory-service 全局配置模型。

    Attributes:
        database_url: 运行时异步连接串（asyncpg 驱动）。
        database_sync_url: alembic 迁移用同步串；为空时自动转换。
        jwt_secret: JWT 签名密钥，生产环境必须为强随机值。
        vector_store: 向量后端选择（pgvector / milvus）。
        context_budget_profile: 上下文预算 profile 名（A/B 实验入口）。
    """

    model_config = SettingsConfigDict(
        env_prefix="MEMORY_SERVICE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 应用 ----
    app_name: str = "EchoDesk memory-service"
    host: str = "0.0.0.0"
    port: int = 8100
    log_level: str = "INFO"

    # ---- 数据库 ----
    database_url: str = "postgresql+asyncpg://workbench:workbench@localhost:5432/workbench"
    database_sync_url: str | None = None
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # ---- Redis ----
    redis_url: str = "redis://localhost:6379/0"

    # ---- 认证（M-02 使用）----
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 30
    refresh_expire_days: int = 7

    # ---- LLM（M-04/M-08 使用）----
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    extractor_model: str | None = None
    embedding_model: str = "BAAI/bge-m3"
    llm_timeout_seconds: float = 60.0  # 单次 LLM 调用超时（含流式整体）
    llm_max_retries: int = 1  # 上游 5xx/网络错误重试次数

    # ---- Embedding（M-04 起用；OpenAI 兼容 /embeddings）----
    embedding_base_url: str | None = None  # 如 http://embedding:7997（infinity 服务）
    embedding_api_key: str | None = None  # 本地服务无需鉴权，云 API 时填写
    embedding_timeout_seconds: float = 30.0
    embedding_max_retries: int = 1
    embedding_batch_size: int = 16  # 单批条数上限：大批量分批发送，防单请求超时并占死本地推理队列
    embedding_dim: int = 1024  # bge-m3 dense 维度（与模型解耦的部署约定）

    # ---- Rerank（M-07 RAG 精排；infinity /rerank，不可用降级 RRF 直排）----
    rerank_base_url: str | None = None  # None 时复用 embedding_base_url
    rerank_model: str = "BAAI/bge-reranker-large"  # infinity 注册名（路径式名会被 400 拒绝）
    rerank_timeout_seconds: float = 30.0
    rerank_max_retries: int = 1

    # ---- RAG 知识库（M-07）----
    rag_chunk_tokens: int = 512  # 切片目标 token 数
    rag_chunk_overlap_ratio: float = 0.1  # 相邻切片重叠比例（10%）
    rag_candidate_k: int = 40  # 向量/BM25 各路召回候选数（融合前）
    rag_top_k: int = 6  # 最终注入 RAG 区块的片段数
    rag_max_upload_mb: int = 20  # 单文件上传上限（MB）

    # ---- 工具调用（M-09）----
    tool_confirm_timeout_seconds: float = 60.0  # external 风险确认等待窗口（超时拒绝）
    tool_result_max_chars: int = 2000  # 工具结果摘要化上限（超出截断标注）
    web_fetch_timeout_seconds: float = 15.0  # web.fetch 单次抓取超时
    web_fetch_max_bytes: int = 524288  # web.fetch 响应体上限（512KB，防超大页面）

    # ---- MCP 外部工具（M-08 M3）----
    # server 配置 JSON 数组，如 [{"name":"fs","transport":"http","url":"http://host:9000/mcp"}]
    mcp_servers_json: str = ""
    mcp_tool_risk: str = "external"  # MCP 工具默认风险分级（D1：最保守，可放宽为 write）
    mcp_connect_timeout_seconds: float = 10.0  # 启动期单 server 连接超时
    mcp_call_timeout_seconds: float = 60.0  # 单次 MCP 工具调用超时

    # ---- 任务与待办（M-10）----
    task_brief_limit: int = 5  # 新会话开场注入的未完成任务条数上限
    task_progress_ttl_days: int = 30  # 任务进度记忆 TTL（docs/01 §5.7：进度类事实 30 天）

    # ---- 记忆抽取/检索（M-04）----
    extract_min_confidence: float = 0.5  # 低于该置信度的抽取结果丢弃（控噪）
    dedup_similarity_threshold: float = 0.92  # 向量相似度判重阈值
    # 疑似冲突召回阈值（低于判重阈值：仅送仲裁、禁 MERGE，Fix A）
    conflict_similarity_threshold: float = 0.80
    # CONFLICTED（待用户裁决）条目参与检索的综合分惩罚系数（评测优化轮补丁：
    # Fix A 副作用修复——待裁决条目若被检索排除，信息会从上下文静默消失；
    # 降权使其排在同条件 ACTIVE 之后，预算紧张时优先被裁掉）
    conflicted_retrieval_penalty: float = 0.6
    # 向量召回候选数（重排前；20→50：fix_v13 分桶取证 27/65 FAIL 目标不在候选 20 内）
    retrieval_candidate_k: int = 50
    retrieval_top_k: int = 8  # 最终返回条数（分区保底见 retriever._balanced_select）
    task_brief_max_session_messages: int = 6  # 任务简报仅在会话早期注入（消息总数 ≤ 该值）

    # ---- 向量库 ----
    vector_store: str = "pgvector"

    # ---- 观测（M-12 使用）----
    langfuse_public_host: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None

    # ---- 上下文工程 ----
    context_budget_profile: str = "default"
    # 预算 YAML 目录（M-05）；None 时自动探测 CWD/仓库根的 config/budgets
    budget_config_dir: str | None = None
    # 价目 YAML 目录（M-12 cost_usd 折算）；None 时自动探测 CWD/仓库根的 config
    pricing_config_dir: str | None = None
    # Working Memory 窗口 token 预算（M-05 Context Builder 接管前 M1/M-06 共用）
    context_window_tokens: int = 4000
    # 滚动摘要自身的 token 上限，超出触发二级压缩（M-06）
    rolling_summary_max_tokens: int = 800

    @property
    def sync_database_url(self) -> str:
        """返回 alembic 使用的同步连接串。

        优先取显式配置；否则将 asyncpg 驱动后缀替换为 psycopg。
        """
        if self.database_sync_url:
            return self.database_sync_url
        return self.database_url.replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    """返回进程级单例配置（避免每请求重复解析环境变量）。"""
    return Settings()
