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
    embedding_dim: int = 1024  # bge-m3 dense 维度（与模型解耦的部署约定）

    # ---- 记忆抽取/检索（M-04）----
    extract_min_confidence: float = 0.5  # 低于该置信度的抽取结果丢弃（控噪）
    dedup_similarity_threshold: float = 0.92  # 向量相似度判重阈值
    retrieval_candidate_k: int = 20  # 向量召回候选数（重排前）
    retrieval_top_k: int = 8  # 最终返回条数

    # ---- 向量库 ----
    vector_store: str = "pgvector"

    # ---- 观测（M-12 使用）----
    langfuse_public_host: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None

    # ---- 上下文工程 ----
    context_budget_profile: str = "default"
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
