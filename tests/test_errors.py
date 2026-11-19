"""统一错误体：所有异常出口形状一致，HTTP 语义化。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import make_client


def assert_error_shape(body: dict, code: str) -> None:
    assert set(body) == {"error"}
    err = body["error"]
    assert {"code", "message", "detail", "request_id"} <= set(err)
    assert err["code"] == code
    assert isinstance(err["message"], str) and err["message"]


def test_unknown_path_returns_unified_404(client: TestClient) -> None:
    resp = client.get("/api/v1/does-not-exist")
    assert resp.status_code == 404
    assert_error_shape(resp.json(), "NOT_FOUND")
    # 默认 HTTP 短语翻成中文，不把 "Not Found" 直接甩给前端
    assert resp.json()["error"]["message"] == "资源不存在"
    # 错误体里的 request_id 与响应头一致，才能反查日志
    assert resp.json()["error"]["request_id"] == resp.headers["X-Request-ID"]


def test_method_not_allowed_maps_code(client: TestClient) -> None:
    resp = client.delete("/healthz")
    assert resp.status_code == 405
    assert_error_shape(resp.json(), "METHOD_NOT_ALLOWED")


def test_rate_limit_returns_429() -> None:
    with make_client(env="dev", rate_limit_per_minute=2) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/healthz").status_code == 200
        resp = client.get("/healthz")
        assert resp.status_code == 429
        assert_error_shape(resp.json(), "TOO_MANY_REQUESTS")
        assert resp.json()["error"]["detail"]["retry_after"] >= 1
        assert "Retry-After" in resp.headers
        # 被限流的响应同样带 request_id
        assert resp.headers["X-Request-ID"]


def test_rate_limit_disabled_in_test_env(client: TestClient) -> None:
    """测试环境默认不限流，否则并发用例会互相踩。"""
    for _ in range(5):
        assert client.get("/healthz").status_code == 200
