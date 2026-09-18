"""Alembic 迁移环境。

连接串从应用 Settings 读取（同步驱动），metadata 来源为
app.db.base（其中集中导入了全部模型模块）。
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.core.config import get_settings
from app.db.base import Base

# ---- Alembic Config 装载 ----
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 应用模型元数据（autogenerate 的对比基准）
target_metadata = Base.metadata

# 用应用配置覆盖 alembic.ini 中的占位连接串
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.sync_database_url)


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL 脚本不连库（本项当前不使用，保留标准模板）。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行迁移（本项目默认路径）。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
