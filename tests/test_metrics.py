"""指标验收：空库、有数据、Prometheus 文本、两个端点。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.events import EventType, emit
from app.models import TaskField, TaskStatus
from app.observability import collect, prometheus_text
from app.store import Database
from tests.conftest import make_client, make_task


def seed(db: Database) -> None:
    now = datetime.now(UTC)
    with db.session() as session:
        done = make_task(original_filename="a.pdf")
        done.status = TaskStatus.DONE.value
        done.created_at = now - timedelta(seconds=10)
        done.updated_at = now
        failed = make_task(original_filename="b.pdf")
        failed.status = TaskStatus.FAILED.value
        failed.created_at = now - timedelta(seconds=20)
        failed.updated_at = now
        pending = make_task(original_filename="c.pdf")
        session.add(done)
        session.add(failed)
        session.add(pending)
        session.flush()

        session.add(TaskField(task_id=done.id, field_key="party_a", value="甲公司", verified=True))
        session.add(
            TaskField(
                task_id=done.id,
                field_key="deposit_ratio",
                value="5",
                needs_review=True,
                verified=False,
            )
        )
        emit(session, done.id, EventType.EXTRACTED, {})
        emit(session, done.id, EventType.RETRY, {})
        emit(session, failed.id, EventType.FAILED, {"code": "TIMEOUT"})


def test_empty_database_metrics(db: Database) -> None:
    with db.session() as session:
        metrics = collect(session)

    assert metrics.tasks_total == 0
    assert metrics.success_rate is None
    assert metrics.review_rate is None
    assert metrics.avg_duration_seconds is None
    assert metrics.fields_total == 0


def test_task_counts_and_rate(db: Database) -> None:
    seed(db)
    with db.session() as session:
        metrics = collect(session)

    assert metrics.tasks_total == 3
    assert metrics.tasks_by_status[TaskStatus.PENDING.value] == 1
    assert metrics.success_total == 1
    assert metrics.failed_total == 1
    # 成功 / (成功+失败+阻塞)
    assert metrics.success_rate == 0.5


def test_duration_stats(db: Database) -> None:
    seed(db)
    with db.session() as session:
        metrics = collect(session)

    assert metrics.avg_duration_seconds is not None
    assert 14.0 <= metrics.avg_duration_seconds <= 16.0
    assert metrics.p95_duration_seconds is not None


def test_field_stats(db: Database) -> None:
    seed(db)
    with db.session() as session:
        metrics = collect(session)

    assert metrics.fields_total == 2
    assert metrics.fields_needs_review == 1
    assert metrics.fields_verified == 1
    assert metrics.fields_rejected == 1
    assert metrics.review_rate == 0.5


def test_event_stats(db: Database) -> None:
    seed(db)
    with db.session() as session:
        metrics = collect(session)

    assert metrics.events_by_type[EventType.EXTRACTED.value] == 1
    assert metrics.retry_total == 1


def test_prometheus_text_format(db: Database) -> None:
    seed(db)
    with db.session() as session:
        text = prometheus_text(collect(session))

    assert "contract_tasks_total 3" in text
    assert "# TYPE contract_tasks_total gauge" in text
    assert 'contract_tasks_status{status="done"} 1' in text
    assert text.endswith("\n")
    # 每行必须是「指标名 值」或注释或带标签的行
    for line in text.strip().splitlines():
        assert line.startswith("#") or " " in line


def test_metrics_endpoint_public(api_client: TestClient, file_db: Database) -> None:
    """抓指标不该要凭证，否则监控接不上。"""
    resp = api_client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "contract_tasks_total" in resp.text


def test_metrics_json_endpoint(api_client: TestClient, file_db: Database) -> None:
    seed(file_db)
    body = api_client.get("/api/v1/metrics").json()

    assert body["tasks"]["total"] == 3
    assert body["fields"]["total"] == 2
    assert body["events"]["retries"] == 1


def test_metrics_json_requires_auth(file_db: Database) -> None:
    from app.store import set_database

    set_database(file_db)
    with make_client(env="dev", api_token="static-xyz") as client:
        assert client.get("/api/v1/metrics").status_code == 401
        ok = client.get("/api/v1/metrics", headers={"X-API-Key": "static-xyz"})
        assert ok.status_code == 200


def test_terminal_task_without_timestamps(db: Database) -> None:
    """极端数据不该让聚合崩掉。"""
    with db.session() as session:
        task = make_task()
        task.status = TaskStatus.DONE.value
        session.add(task)
        session.flush()
        assert collect(session).tasks_total == 1
