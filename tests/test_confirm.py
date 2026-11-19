"""确认接口验收：一次性 token、四道闸门、人工修改落库、二次提交冲突。"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.models import Task, TaskField, TaskStatus
from app.runner import TaskRunner, load_fields
from app.store import Database
from tests.conftest import make_task
from tests.test_runner import FakeGraph, make_result


def make_token_runner(db: Database, result: dict[str, Any] | None = None) -> TaskRunner:
    settings = Settings(_env_file=None, env="test", runner_workers=0)
    return TaskRunner(
        database=db, graph=FakeGraph(result or make_result("await_confirm")), settings=settings
    )


def seed_await_confirm(client: TestClient, db: Database) -> tuple[str, str]:
    """造一条已经跑到 await_confirm 的任务，返回 (task_id, confirm_token)。"""
    with db.session() as session:
        task = make_task()
        task.status = TaskStatus.AWAIT_CONFIRM.value
        task.confirm_token = "tok-" + "a" * 20
        session.add(task)
        session.flush()
        task_id = task.id
        session.add(
            TaskField(
                task_id=task_id,
                field_key="deposit_ratio",
                value="3",
                confidence=0.4,
                needs_review=True,
                source_clause_id="DOC901-C0008",
            )
        )
        session.add(TaskField(task_id=task_id, field_key="party_a", value="甲方", confidence=0.95))
    return task_id, "tok-" + "a" * 20


# ---------- 正常路径 ----------


def test_confirm_moves_to_done(api_client: TestClient, file_db: Database) -> None:
    task_id, token = seed_await_confirm(api_client, file_db)

    resp = api_client.post(f"/api/v1/tasks/{task_id}/confirm", json={"confirm_token": token})

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "done"


def test_confirm_applies_human_edit(api_client: TestClient, file_db: Database) -> None:
    task_id, token = seed_await_confirm(api_client, file_db)

    api_client.post(
        f"/api/v1/tasks/{task_id}/confirm",
        json={
            "confirm_token": token,
            "fields": [{"field_key": "deposit_ratio", "value": "5"}],
        },
    )

    with file_db.session() as session:
        fields = {f.field_key: f for f in load_fields(session, task_id)}
    assert fields["deposit_ratio"].value == "5"
    assert fields["deposit_ratio"].needs_review is False
    assert fields["deposit_ratio"].verified is True
    assert fields["deposit_ratio"].verify_note == "human_confirmed"
    # 没改的字段也视为人工确认过
    assert fields["party_a"].verified is True


def test_confirm_records_token_usage(api_client: TestClient, file_db: Database) -> None:
    task_id, token = seed_await_confirm(api_client, file_db)
    api_client.post(f"/api/v1/tasks/{task_id}/confirm", json={"confirm_token": token})

    with file_db.session() as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.confirm_token_used_at is not None


def test_confirm_clears_blocked_reason(api_client: TestClient, file_db: Database) -> None:
    task_id, token = seed_await_confirm(api_client, file_db)
    with file_db.session() as session:
        task = session.get(Task, task_id)
        assert task is not None
        task.blocked_reason = "字段校验未通过：deposit_ratio"
        session.add(task)

    api_client.post(f"/api/v1/tasks/{task_id}/confirm", json={"confirm_token": token})

    with file_db.session() as session:
        assert session.get(Task, task_id).blocked_reason is None


def test_detail_exposes_fields_and_token(api_client: TestClient, file_db: Database) -> None:
    """确认卡要渲染字段和 token，详情接口一次给全。"""
    task_id, token = seed_await_confirm(api_client, file_db)

    detail = api_client.get(f"/api/v1/tasks/{task_id}").json()

    assert detail["confirm_token"] == token
    assert {f["field_key"] for f in detail["fields"]} == {"deposit_ratio", "party_a"}


# ---------- 闸门 ----------


def test_wrong_token_forbidden(api_client: TestClient, file_db: Database) -> None:
    task_id, _ = seed_await_confirm(api_client, file_db)

    resp = api_client.post(
        f"/api/v1/tasks/{task_id}/confirm", json={"confirm_token": "wrong-token-123"}
    )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_second_confirm_conflicts(api_client: TestClient, file_db: Database) -> None:
    """一次性凭证：用过就废，二次提交必须 409。"""
    task_id, token = seed_await_confirm(api_client, file_db)
    first = api_client.post(f"/api/v1/tasks/{task_id}/confirm", json={"confirm_token": token})
    assert first.status_code == 200

    second = api_client.post(f"/api/v1/tasks/{task_id}/confirm", json={"confirm_token": token})

    assert second.status_code == 409
    assert second.json()["error"]["code"] in {"CONFIRM_ALREADY_USED", "TASK_NOT_AWAIT_CONFIRM"}


def test_confirm_pending_task_conflicts(api_client: TestClient, file_db: Database) -> None:
    with file_db.session() as session:
        task = make_task()
        session.add(task)
        session.flush()
        task_id = task.id

    resp = api_client.post(
        f"/api/v1/tasks/{task_id}/confirm", json={"confirm_token": "tok-" + "a" * 20}
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "TASK_NOT_AWAIT_CONFIRM"


def test_confirm_missing_task_404(api_client: TestClient, file_db: Database) -> None:
    resp = api_client.post(
        "/api/v1/tasks/no-such-task/confirm", json={"confirm_token": "tok-" + "a" * 20}
    )
    assert resp.status_code == 404


def test_unknown_field_rejected(api_client: TestClient, file_db: Database) -> None:
    task_id, token = seed_await_confirm(api_client, file_db)

    resp = api_client.post(
        f"/api/v1/tasks/{task_id}/confirm",
        json={
            "confirm_token": token,
            "fields": [{"field_key": "project_manager_phone", "value": "123"}],
        },
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "BAD_REQUEST"


def test_stale_version_conflicts(api_client: TestClient, file_db: Database) -> None:
    task_id, token = seed_await_confirm(api_client, file_db)

    resp = api_client.post(
        f"/api/v1/tasks/{task_id}/confirm",
        json={"confirm_token": token, "version": 99},
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONCURRENT_MODIFICATION"


def test_short_token_rejected_by_schema(api_client: TestClient, file_db: Database) -> None:
    task_id, _ = seed_await_confirm(api_client, file_db)
    resp = api_client.post(f"/api/v1/tasks/{task_id}/confirm", json={"confirm_token": "short"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------- 与执行器衔接 ----------


@pytest.mark.asyncio
async def test_runner_issues_token_on_await_confirm(db: Database) -> None:
    runner = make_token_runner(db)
    with db.session() as session:
        task = make_task()
        session.add(task)
        session.flush()
        task_id = task.id

    outcome = await runner.run_task(task_id)

    assert outcome.status == TaskStatus.AWAIT_CONFIRM.value
    assert outcome.confirm_token
    with db.session() as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.confirm_token == outcome.confirm_token


@pytest.mark.asyncio
async def test_runner_does_not_issue_token_when_done(db: Database) -> None:
    runner = make_token_runner(db, make_result("done", review_keys=[], verified={}))
    with db.session() as session:
        task = make_task()
        session.add(task)
        session.flush()
        task_id = task.id

    outcome = await runner.run_task(task_id)

    assert outcome.status == TaskStatus.DONE.value
    assert outcome.confirm_token is None
