"""存储层：引擎、会话、建表。

生产用 Alembic 迁移建表，`create_all()` 只给测试和本地空库用——
它能建出新表，但不会改老表，拿它当迁移工具迟早在线上翻车。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

from app.config import get_settings
from app.models import Task, TaskField, TaskTransition  # noqa: F401  建表时要用到


def _connect_args(url: str) -> dict[str, Any]:
    # FastAPI 的线程池会跨线程用一个连接，SQLite 默认报警
    if url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


def _ensure_sqlite_dir(url: str) -> None:
    """`sqlite:///./data/app.db` 的父目录不存在时，sqlite 会直接报 unable to open。"""
    if not url.startswith("sqlite:///"):
        return
    db_path = url.removeprefix("sqlite:///")
    if not db_path or db_path == ":memory:":
        return
    Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def create_db_engine(url: str) -> Engine:
    _ensure_sqlite_dir(url)
    kwargs: dict[str, Any] = {}
    if url in {"sqlite://", "sqlite:///:memory:"} or ":memory:" in url:
        # 内存库默认每个连接一个独立数据库，测试里必须共用同一条连接
        kwargs["poolclass"] = StaticPool
    engine = create_engine(
        url,
        connect_args=_connect_args(url),
        pool_pre_ping=True,
        future=True,
        **kwargs,
    )

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragma(dbapi_connection: sqlite3.Connection, _record: Any) -> None:
            # SQLite 默认不校验外键、也不开 WAL；这两项都要显式打开
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


class Database:
    """把引擎与会话工厂包一层，避免每个调用点都自己 sessionmaker。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self.engine = create_db_engine(url)
        self._session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,  # commit 后还要读属性，关了省一堆额外查询
        )

    @contextmanager
    def session(self) -> Iterator[Session]:
        """一次事务：正常退出 commit，异常回滚。调用方不用写 try/finally。"""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def create_all(self) -> None:
        SQLModel.metadata.create_all(self.engine)

    def drop_all(self) -> None:
        SQLModel.metadata.drop_all(self.engine)

    def dispose(self) -> None:
        self.engine.dispose()


@contextmanager
def session_scope() -> Iterator[Session]:
    """不关心 Database 实例时的快捷入口，走进程单例。"""
    with get_database().session() as session:
        yield session


_database: Database | None = None


def get_database() -> Database:
    """进程单例。测试或后续依赖注入可用 set_database 替换。"""
    global _database
    if _database is None:
        _database = Database(get_settings().database_url)
    return _database


def set_database(database: Database | None) -> None:
    global _database
    _database = database


def reset_database() -> None:
    """释放旧引擎。换连接串或跑完测试清理时调用。"""
    global _database
    if _database is not None:
        _database.dispose()
    _database = None


__all__ = [
    "Database",
    "create_db_engine",
    "get_database",
    "reset_database",
    "session_scope",
    "set_database",
]
