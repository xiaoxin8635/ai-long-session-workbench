"""Agent 图的 checkpoint 持久化（M-08 M3，docs/06 §10 DoD：中断恢复）。

设计要点：
  - 生产：AsyncPostgresSaver（langgraph 官方 checkpoint 存储，psycopg 异步池），
    thread_id = "<workspace_id>:<session_id>"——同一会话的图状态跨请求/跨进程可恢复
  - 降级：PG 不可达或初始化失败时回落 InMemorySaver（进程内，重启丢 thread，
    resume 端点报错但对话链路不受影响——外部依赖降级原则）
  - checkpoint 表由 LangGraph setup() 自建（checkpoints/checkpoint_migrations 等
    前缀表，与业务表无外键关联，不纳入 Alembic 管理——生态标准做法）
"""

import logging

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# LangGraph checkpoint 表专用 schema：业务库已有 M-06 的摘要检查点表
# checkpoints（public schema），LangGraph 的同名表（thread_id 主键结构）
# 若同落 public 会被 IF NOT EXISTS 静默跳过、随后建索引报列不存在——
# 经连接级 search_path 隔离到独立 schema（表/迁移记录整体落这里）
_CHECKPOINT_SCHEMA = "agent_ckpt"


class AgentRuntime:
    """Agent 图运行时：checkpointer 生命周期与编译图的进程级缓存。

    Attributes:
        _pool: psycopg 异步连接池（PG 模式）；None 表示降级内存模式。
        _checkpointer: LangGraph checkpoint 存储。
        _graph: 编译后的图（start 后惰性构建，进程内复用）。
    """

    def __init__(self) -> None:
        """初始化未启动的运行时（start 前不可用）。"""
        self._pool: AsyncConnectionPool | None = None
        self._checkpointer: BaseCheckpointSaver | None = None
        self._graph: object | None = None

    async def start(self) -> None:
        """启动 checkpointer（FastAPI lifespan startup 调用；失败降级内存模式）。

        PG 模式下 setup() 幂等建表；连接池连接必须 autocommit=True
        （langgraph 官方约定：checkpoint migration 含 CREATE INDEX
        CONCURRENTLY，事务块内执行会失败）；连接失败记 warning 后回落
        InMemorySaver，保证服务可启动（对话链路可用，仅中断恢复能力降级）。
        """
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        settings = get_settings()
        conninfo = settings.database_url.replace("+asyncpg", "")
        pool: AsyncConnectionPool | None = None
        try:
            pool = AsyncConnectionPool(
                conninfo,
                min_size=1,
                max_size=4,
                open=False,
                # 官方 checkpoint 连接语义（autocommit）：setup 的
                # CONCURRENTLY 索引与 pipeline 写入都要求事务外执行
                kwargs={
                    "autocommit": True,
                    # LangGraph 表落专用 schema（模块注释：与业务 checkpoints 表隔离）
                    "options": f"-c search_path={_CHECKPOINT_SCHEMA}",
                },
            )
            await pool.open()  # 构造器 open=True 已废弃（psycopg_pool 弃用告警）
            # schema 幂等建立（sql.Identifier 参数化，非字符串拼接）
            async with pool.connection() as conn:
                await conn.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                        sql.Identifier(_CHECKPOINT_SCHEMA)
                    )
                )
            checkpointer: BaseCheckpointSaver = AsyncPostgresSaver(pool)
            await checkpointer.setup()  # 幂等：CREATE TABLE IF NOT EXISTS
            self._pool = pool
            self._checkpointer = checkpointer
            logger.info("agent_checkpoint_backend=postgres")
        except Exception as exc:  # PG 不可达：降级内存 checkpoint
            logger.warning("agent_checkpoint_degraded error=%s", exc)
            if pool is not None:  # 回收半启动的池，防 worker task 悬挂
                try:
                    await pool.close()
                except Exception:
                    pass
            self._checkpointer = InMemorySaver()

    async def close(self) -> None:
        """关闭连接池（FastAPI lifespan shutdown 调用；内存模式为 no-op）。"""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        self._checkpointer = None
        self._graph = None

    def build_graph(self) -> object:
        """构建（或返回缓存）编译后的图。

        Returns:
            LangGraph CompiledStateGraph（含 checkpointer；类型经运行时封装，
            调用方仅用 ainvoke 接口）。
        """
        if self._graph is None:
            from app.agent.graph import build_graph

            self._graph = build_graph(self._checkpointer)
        return self._graph


# 进程级单例（main.py lifespan 启动；路由经 get_agent_runtime 取用）
_agent_runtime = AgentRuntime()


def get_agent_runtime() -> AgentRuntime:
    """返回进程级 Agent 运行时单例。

    Returns:
        AgentRuntime 实例（start 前调用 build_graph 会以无 checkpointer 编译）。
    """
    return _agent_runtime
