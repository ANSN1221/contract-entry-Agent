"""鉴权验收：签名校验、过期、篡改、静态 token、生产 fail closed。"""

from __future__ import annotations

import pytest

from app.auth import (
    Principal,
    issue_token,
    verify_token,
)
from app.errors import Unauthorized
from tests.conftest import make_client

SECRET = "test-secret-0123456789abcdef"
TASK_BODY = {
    "original_filename": "合同.pdf",
    "file_sha256": "a" * 64,
    "file_path": "uploads/a.pdf",
}


# ---------- token 编解码 ----------


def test_issue_and_verify_roundtrip() -> None:
    token = issue_token("alice", secret=SECRET, ttl_seconds=600, now=1_700_000_000)
    principal = verify_token(token, secret=SECRET, now=1_700_000_100)

    assert isinstance(principal, Principal)
    assert principal.subject == "alice"
    assert principal.via == "signed"
    assert principal.expires_at == 1_700_000_600


def test_expired_token_rejected() -> None:
    token = issue_token("alice", secret=SECRET, ttl_seconds=60, now=1_700_000_000)
    with pytest.raises(Unauthorized) as exc:
        verify_token(token, secret=SECRET, now=1_700_000_061)
    assert "过期" in exc.value.message


def test_token_barely_valid_at_boundary() -> None:
    token = issue_token("alice", secret=SECRET, ttl_seconds=60, now=1_700_000_000)
    assert verify_token(token, secret=SECRET, now=1_700_000_059).subject == "alice"


def test_tampered_payload_rejected() -> None:
    token = issue_token("alice", secret=SECRET, ttl_seconds=600, now=1_700_000_000)
    prefix, body, signature = token.split(".")
    forged = f"{prefix}.{body[:-2]}AA.{signature}"
    with pytest.raises(Unauthorized):
        verify_token(forged, secret=SECRET)


def test_wrong_secret_rejected() -> None:
    token = issue_token("alice", secret=SECRET, ttl_seconds=600)
    with pytest.raises(Unauthorized):
        verify_token(token, secret="another-secret-0987654321")


def test_malformed_token_rejected() -> None:
    for bad in ("", "abc", "v2.aaa.bbb", "v1.only-two"):
        with pytest.raises(Unauthorized):
            verify_token(bad, secret=SECRET)


def test_short_secret_refused() -> None:
    with pytest.raises(ValueError):
        issue_token("alice", secret="short", ttl_seconds=60)


# ---------- HTTP 层 ----------


def test_dev_without_credentials_allows_request() -> None:
    """dev/test 未配置凭证时放行，否则本地开发每次都要造 token。"""
    with make_client(env="dev") as client:
        assert client.get("/api/v1/tasks").status_code == 200


def test_signed_token_required_when_secret_configured() -> None:
    with make_client(env="dev", api_token_secret=SECRET) as client:
        assert client.get("/api/v1/tasks").status_code == 401

        token = issue_token("alice", secret=SECRET, ttl_seconds=600)
        resp = client.get("/api/v1/tasks", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200


def test_missing_token_error_body() -> None:
    with make_client(env="dev", api_token_secret=SECRET) as client:
        resp = client.get("/api/v1/tasks")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "UNAUTHORIZED"
        # 401 要带 WWW-Authenticate，客户端才知道该用 Bearer
        assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_expired_token_rejected_over_http() -> None:
    with make_client(env="dev", api_token_secret=SECRET) as client:
        token = issue_token("alice", secret=SECRET, ttl_seconds=-10)
        resp = client.get("/api/v1/tasks", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert "过期" in resp.json()["error"]["message"]


def test_static_token_accepted() -> None:
    with make_client(env="dev", api_token="static-token-123") as client:
        assert client.get("/api/v1/tasks").status_code == 401
        ok = client.get("/api/v1/tasks", headers={"X-API-Key": "static-token-123"})
        assert ok.status_code == 200


def test_static_and_signed_coexist() -> None:
    with make_client(env="dev", api_token="static-token-123", api_token_secret=SECRET) as c:
        signed = issue_token("bob", secret=SECRET, ttl_seconds=600)
        bearer = {"Authorization": f"Bearer {signed}"}
        assert c.get("/api/v1/tasks", headers=bearer).status_code == 200
        assert c.get("/api/v1/tasks", headers={"X-API-Key": "static-token-123"}).status_code == 200


def test_healthz_stays_public() -> None:
    """探针不能要凭证，否则部署平台探活全部失败。"""
    with make_client(env="dev", api_token_secret=SECRET) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200


def test_prod_without_credentials_fails_closed() -> None:
    """生产环境忘配凭证必须全拒，绝不能裸奔。"""
    with make_client(env="prod", api_token="", api_token_secret="") as client:
        resp = client.get("/api/v1/tasks")
        assert resp.status_code == 401
        assert "未配置" in resp.json()["error"]["message"]


def test_protected_route_writes_require_auth() -> None:
    with make_client(env="dev", api_token_secret=SECRET) as client:
        assert client.post("/api/v1/tasks", json=TASK_BODY).status_code == 401
        token = issue_token("dev", secret=SECRET, ttl_seconds=600)
        resp = client.post(
            "/api/v1/tasks", json=TASK_BODY, headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 201


def test_garbage_authorization_header_rejected() -> None:
    with make_client(env="dev", api_token_secret=SECRET) as client:
        resp = client.get("/api/v1/tasks", headers={"Authorization": "Bearer not-a-token"})
        assert resp.status_code == 401
        resp2 = client.get("/api/v1/tasks", headers={"Authorization": "Basic abc"})
        assert resp2.status_code == 401


def test_openapi_declares_bearer_scheme() -> None:
    """/docs 上要有 Authorize 按钮，演示时才能现场填 token。"""
    with make_client(env="dev", api_token_secret=SECRET) as client:
        schema = client.get("/openapi.json").json()
        schemes = schema["components"]["securitySchemes"]
        assert any(item.get("scheme") == "bearer" for item in schemes.values())
