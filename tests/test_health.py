"""骨架验收：服务起得来、探针通、request_id 贯穿。"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz_ok(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "contract-entry-agent"
    assert body["env"] == "test"


def test_readyz_ok(client: TestClient) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


def test_request_id_generated(client: TestClient) -> None:
    resp = client.get("/healthz")
    request_id = resp.headers.get("X-Request-ID")
    assert request_id and len(request_id) == 16


def test_request_id_passthrough(client: TestClient) -> None:
    """客户端自带 request_id 时要原样透传，否则跨服务链路会断。"""
    resp = client.get("/healthz", headers={"X-Request-ID": "trace-abc-123"})
    assert resp.headers["X-Request-ID"] == "trace-abc-123"


def test_oversized_request_id_replaced(client: TestClient) -> None:
    """外部传入的超长串不信任，避免污染日志。"""
    resp = client.get("/healthz", headers={"X-Request-ID": "x" * 200})
    assert resp.headers["X-Request-ID"] != "x" * 200
    assert len(resp.headers["X-Request-ID"]) == 16


def test_openapi_available(client: TestClient) -> None:
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    assert "/healthz" in paths


def test_server_timing_header(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert "dur=" in resp.headers["Server-Timing"]
