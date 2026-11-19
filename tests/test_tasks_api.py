"""任务 API 契约验收：创建、幂等、查询、分页、状态迁移与统一错误体。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.models import TaskStatus


def _payload(**overrides) -> dict:
    data = {
        "original_filename": "采购合同-2026-001.pdf",
        "file_sha256": "a" * 64,
        "file_path": "uploads/采购合同-2026-001.pdf",
    }
    data.update(overrides)
    return data


def _create(client: TestClient, **overrides) -> dict:
    resp = client.post("/api/v1/tasks", json=_payload(**overrides))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _transition(client: TestClient, task_id: str, to_status: str, version: int) -> dict:
    return client.post(
        f"/api/v1/tasks/{task_id}/transitions",
        json={"to_status": to_status, "version": version},
    )


class TestCreateTask:
    def test_create_returns_201_with_pending_task(self, api_client: TestClient) -> None:
        resp = api_client.post("/api/v1/tasks", json=_payload())

        assert resp.status_code == 201
        task = resp.json()
        assert len(task["id"]) == 32
        assert task["status"] == TaskStatus.PENDING.value
        assert task["version"] == 0
        assert task["original_filename"] == "采购合同-2026-001.pdf"
        assert task["created_at"]

    def test_missing_file_metadata_rejected_400(self, api_client: TestClient) -> None:
        resp = api_client.post("/api/v1/tasks", json={"original_filename": "x.pdf"})

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_bad_sha256_rejected_400(self, api_client: TestClient) -> None:
        resp = api_client.post("/api/v1/tasks", json=_payload(file_sha256="not-a-hash"))

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_idempotency_key_replay_returns_same_task(self, api_client: TestClient) -> None:
        headers = {"Idempotency-Key": "imp-001"}
        first = api_client.post("/api/v1/tasks", json=_payload(), headers=headers)
        second = api_client.post("/api/v1/tasks", json=_payload(), headers=headers)

        assert first.status_code == 201
        assert second.status_code == 200
        assert second.json()["id"] == first.json()["id"]

        listed = api_client.get("/api/v1/tasks").json()
        assert listed["total"] == 1


class TestGetTask:
    def test_get_returns_task_with_empty_timeline(self, api_client: TestClient) -> None:
        task = _create(api_client)

        resp = api_client.get(f"/api/v1/tasks/{task['id']}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == task["id"]
        assert body["transitions"] == []

    def test_get_unknown_returns_404_unified_body(self, api_client: TestClient) -> None:
        resp = api_client.get("/api/v1/tasks/" + "0" * 32)

        assert resp.status_code == 404
        error = resp.json()["error"]
        assert error["code"] == "NOT_FOUND"
        assert error["message"]
        assert error["request_id"]


class TestListTasks:
    def test_list_paginates_with_total(self, api_client: TestClient) -> None:
        for i in range(3):
            _create(api_client, original_filename=f"合同-{i}.pdf")

        page1 = api_client.get("/api/v1/tasks", params={"offset": 0, "limit": 2}).json()
        page2 = api_client.get("/api/v1/tasks", params={"offset": 2, "limit": 2}).json()

        assert len(page1["items"]) == 2
        assert page1["total"] == 3
        assert len(page2["items"]) == 1
        assert page2["total"] == 3

    def test_list_orders_newest_first(self, api_client: TestClient) -> None:
        _create(api_client, original_filename="旧.pdf")
        _create(api_client, original_filename="新.pdf")

        items = api_client.get("/api/v1/tasks").json()["items"]

        assert items[0]["original_filename"] == "新.pdf"

    def test_list_filters_by_status(self, api_client: TestClient) -> None:
        task = _create(api_client)
        _transition(api_client, task["id"], "running", 0)

        running = api_client.get("/api/v1/tasks", params={"status": "running"}).json()
        pending = api_client.get("/api/v1/tasks", params={"status": "pending"}).json()

        assert running["total"] == 1
        assert pending["total"] == 0

    def test_list_rejects_unknown_status_400(self, api_client: TestClient) -> None:
        resp = api_client.get("/api/v1/tasks", params={"status": "flying"})

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


class TestTransition:
    def test_transition_pending_to_running_updates_and_audits(self, api_client: TestClient) -> None:
        task = _create(api_client)

        resp = _transition(api_client, task["id"], "running", 0)

        assert resp.status_code == 200
        assert resp.json()["status"] == TaskStatus.RUNNING.value
        assert resp.json()["version"] == 1

        detail = api_client.get(f"/api/v1/tasks/{task['id']}").json()
        record = detail["transitions"][0]
        assert record["from_status"] == "pending"
        assert record["to_status"] == "running"
        assert record["actor"] == "system"
        assert record["request_id"] == resp.headers["X-Request-ID"]

    def test_transition_illegal_returns_409_invalid_transition(
        self, api_client: TestClient
    ) -> None:
        task = _create(api_client)
        _transition(api_client, task["id"], "running", 0)

        resp = _transition(api_client, task["id"], "pending", 1)

        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "INVALID_TRANSITION"

    def test_transition_stale_version_returns_409_concurrent_modification(
        self, api_client: TestClient
    ) -> None:
        task = _create(api_client)
        _transition(api_client, task["id"], "running", 0)

        resp = _transition(api_client, task["id"], "await_confirm", 0)

        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CONCURRENT_MODIFICATION"

    def test_transition_unknown_task_returns_404(self, api_client: TestClient) -> None:
        resp = _transition(api_client, "0" * 32, "running", 0)

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"

    def test_transition_unknown_status_value_returns_400(self, api_client: TestClient) -> None:
        task = _create(api_client)

        resp = _transition(api_client, task["id"], "flying", 0)

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_terminal_task_rejects_further_transition(self, api_client: TestClient) -> None:
        task = _create(api_client)
        _transition(api_client, task["id"], "running", 0)
        failed = _transition(api_client, task["id"], "failed", 1)
        assert failed.status_code == 200

        resp = _transition(api_client, task["id"], "done", 2)

        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "INVALID_TRANSITION"
