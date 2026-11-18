from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.config import Settings
from app.main import create_app
from app.models import Task
from app.store import Database


def pytest_configure(config: pytest.Config) -> None:
    """测试里不打业务日志，但 structlog 未配置时会回落到裸输出，这里一次性关掉。"""
    structlog.configure(processors=[], logger_factory=structlog.ReturnLoggerFactory())


@pytest.fixture()
def settings() -> Settings:
    return Settings(env="test", log_level="WARNING", rate_limit_per_minute=0)


@pytest.fixture()
def client(settings: Settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture()
def db() -> Iterator[Database]:
    """内存库：每个用例一套干净表结构。"""
    database = Database("sqlite://")
    database.create_all()
    yield database
    database.dispose()


@pytest.fixture()
def file_db(tmp_path: Path) -> Iterator[Database]:
    """文件库：需要多个连接真并发时用（内存库共享单连接，看不出锁行为）。"""
    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    database.create_all()
    yield database
    database.dispose()


@pytest.fixture()
def session(db: Database) -> Iterator[Session]:
    with db.session() as s:
        yield s


def make_task(**overrides) -> Task:
    data = {
        "original_filename": "合同样例.pdf",
        "file_sha256": "a" * 64,
        "file_path": "uploads/sample.pdf",
    }
    data.update(overrides)
    return Task(**data)


def make_client(**overrides) -> TestClient:
    """按需改配置造客户端（限流、鉴权这类要特定配置的用例用）。"""
    base = {"log_level": "WARNING", "rate_limit_per_minute": 0}
    return TestClient(create_app(Settings(**{**base, **overrides})))
