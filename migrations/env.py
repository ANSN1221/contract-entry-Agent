"""Alembic 环境：连接串与元数据都从项目配置来，不在 alembic.ini 里写死。

`render_as_batch=True` 是 SQLite 的必需品：它不支持大部分 ALTER TABLE，
Alembic 会用「建新表 → 拷数据 → 换名」的方式模拟。切到 PostgreSQL 后
这个开关也无害，所以不区分数据库直接开着。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlmodel import SQLModel

# 让 alembic 能 import 到 app 包（命令在任意目录下执行都成立）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import models  # noqa: E402,F401  导入即注册表结构
from app.config import get_settings  # noqa: E402
from app.store import create_db_engine  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 连接串以 Settings 为准：Alembic 与运行时永远指向同一个库
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # 复用 app 的引擎工厂：SQLite 的父目录创建、PRAGMA 设置只维护一份
    connectable = create_db_engine(config.get_main_option("sqlalchemy.url") or "")
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
