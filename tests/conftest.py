from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def settings() -> Settings:
    return Settings(env="test", log_level="WARNING", rate_limit_per_minute=0)


@pytest.fixture()
def client(settings: Settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def make_client(**overrides) -> TestClient:
    """按需改配置造客户端（限流、鉴权这类要特定配置的用例用）。"""
    base = {"log_level": "WARNING", "rate_limit_per_minute": 0}
    return TestClient(create_app(Settings(**{**base, **overrides})))
