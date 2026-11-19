"""事件流验收：事件落库顺序、SSE 帧格式、Last-Event-ID 补发、鉴权通道。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.auth import issue_token
from app.config import Settings
from app.events import EventType, emit, list_events, to_sse
from app.models import Task, TaskStatus
from app.runner import TaskRunner
from app.store import Database
from tests.conftest import make_client, make_task
from tests.test_runner import FakeGraph, make_result

SECRET = "test-secret-0123456789abcdef"


def frames(body: str) -> list[dict]:
    """把 SSE 文本拆成 [{id, event, data}]，顺带验证帧结构。"""
    out: list[dict] = []
    for block in body.strip().split("\n\n"):
        if not block or block.startswith(":"):
            continue
        item: dict = {}
        for line in block.splitlines():
            key, _, value = line.partition(": ")
            if key == "id":
                item["id"] = int(value)
            elif key == "event":
                item["event"] = value
            elif key == "data":
                item["data"] = json.loads(value)
        if "event" in item:
            out.append(item)
    return out


def seed_task(db: Database, **overrides) -> str:
    with db.session() as session:
        task = make_task(**overrides)
        session.add(task)
        session.flush()
        return task.id


# ---------- 落库 ----------


def test_emit_and_list(db: Database) -> None:
    task_id = seed_task(db)
    with db.session() as session:
        first = emit(session, task_id, EventType.QUEUED, {"via": "api"})
        emit(session, task_id, EventType.RUNNING, {})

    with db.session() as session:
        events = list_events(session, task_id)
    assert [e.type for e in events] == ["task.queued", "task.running"]
    assert events[0].id is not None
    assert events[0].id == first.id


def test_list_events_after_cursor(db: Database) -> None:
    task_id = seed_task(db)
    with db.session() as session:
        for index in range(4):
            emit(session, task_id, EventType.RUNNING, {"i": index})
        all_events = list_events(session, task_id)

    with db.session() as session:
        tail = list_events(session, task_id, after_id=all_events[1].id)
    assert [e.payload["i"] for e in tail] == [2, 3]


def test_to_sse_format(db: Database) -> None:
    task_id = seed_task(db)
    with db.session() as session:
        event = emit(session, task_id, EventType.EXTRACTED, {"filled": 11})

    body = to_sse(event)
    assert body.startswith(f"id: {event.id}\n")
    assert "event: extraction.done\n" in body
    assert body.endswith("\n\n")
    # data 必须是单行 JSON，否则浏览器 EventSource 会截断
    data_line = [ln for ln in body.splitlines() if ln.startswith("data: ")][0]
    assert json.loads(data_line[6:])["payload"] == {"filled": 11}


# ---------- HTTP ----------


def test_stream_replays_existing_events(api_client: TestClient, file_db: Database) -> None:
    task_id = seed_task(file_db, status=TaskStatus.DONE.value)
    with file_db.session() as session:
        emit(session, task_id, EventType.RUNNING, {})
        emit(session, task_id, EventType.DONE, {})

    resp = api_client.get(f"/api/v1/tasks/{task_id}/events?follow=false")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    parsed = frames(resp.text)
    assert [f["event"] for f in parsed] == ["task.running", "task.done"]
    assert parsed[0]["data"]["task_id"] == task_id


def test_stream_respects_last_event_id(api_client: TestClient, file_db: Database) -> None:
    task_id = seed_task(file_db, status=TaskStatus.DONE.value)
    with file_db.session() as session:
        for _ in range(3):
            emit(session, task_id, EventType.RUNNING, {})
        events = list_events(session, task_id)

    resp = api_client.get(
        f"/api/v1/tasks/{task_id}/events?follow=false",
        headers={"Last-Event-ID": str(events[0].id)},
    )

    parsed = frames(resp.text)
    assert len(parsed) == 2
    assert parsed[0]["id"] == events[1].id


def test_stream_since_param(api_client: TestClient, file_db: Database) -> None:
    task_id = seed_task(file_db, status=TaskStatus.DONE.value)
    with file_db.session() as session:
        emit(session, task_id, EventType.RUNNING, {})
        emit(session, task_id, EventType.DONE, {})

    resp = api_client.get(f"/api/v1/tasks/{task_id}/events?follow=false&since=1")
    parsed = frames(resp.text)
    assert [f["event"] for f in parsed] == ["task.done"]


def test_stream_unknown_task_404(api_client: TestClient, file_db: Database) -> None:
    resp = api_client.get("/api/v1/tasks/no-such/events?follow=false")
    assert resp.status_code == 404


def test_follow_on_terminal_task_ends_itself(api_client: TestClient, file_db: Database) -> None:
    """终态任务的流要自己收尾，否则前端挂一个永远没数据的连接。"""
    task_id = seed_task(file_db, status=TaskStatus.DONE.value)
    with file_db.session() as session:
        emit(session, task_id, EventType.DONE, {})

    resp = api_client.get(f"/api/v1/tasks/{task_id}/events")

    assert resp.status_code == 200
    assert [f["event"] for f in frames(resp.text)] == ["task.done"]


def test_sse_accepts_token_in_query(file_db: Database) -> None:
    """EventSource 不能自定义请求头，SSE 端点额外允许 ?token=。"""
    token = issue_token("dev", secret=SECRET, ttl_seconds=600)
    with make_client(env="dev", api_token_secret=SECRET) as client:
        from app.store import set_database

        set_database(file_db)
        task_id = seed_task(file_db, status=TaskStatus.DONE.value)
        with file_db.session() as session:
            emit(session, task_id, EventType.DONE, {})

        assert client.get(f"/api/v1/tasks/{task_id}/events?follow=false").status_code == 401
        ok = client.get(f"/api/v1/tasks/{task_id}/events?follow=false&token={token}")
        assert ok.status_code == 200
        assert frames(ok.text)[0]["event"] == "task.done"


# ---------- 与执行器/确认接口衔接 ----------


@pytest.mark.asyncio
async def test_runner_emits_full_sequence(db: Database) -> None:
    runner = TaskRunner(
        database=db,
        graph=FakeGraph(make_result("await_confirm")),
        settings=Settings(_env_file=None, env="test", runner_workers=0),
    )
    task_id = seed_task(db)

    await runner.run_task(task_id)

    with db.session() as session:
        types = [e.type for e in list_events(session, task_id)]
    assert types == [
        EventType.RUNNING.value,
        EventType.EXTRACTED.value,
        EventType.VERIFIED.value,
        EventType.AWAIT_CONFIRM.value,
    ]


@pytest.mark.asyncio
async def test_runner_emits_failed_event(db: Database) -> None:
    runner = TaskRunner(
        database=db,
        graph=FakeGraph(error=RuntimeError("boom")),
        settings=Settings(_env_file=None, env="test", runner_workers=0),
    )
    task_id = seed_task(db)

    await runner.run_task(task_id)

    with db.session() as session:
        types = [e.type for e in list_events(session, task_id)]
        failed = list_events(session, task_id)[-1]
    assert types[-1] == EventType.FAILED.value
    assert failed.payload["code"] == "RUNNER_ERROR"


def test_confirm_emits_confirmed_event(api_client: TestClient, file_db: Database) -> None:
    token = "tok-" + "a" * 20
    task_id = seed_task(file_db, status=TaskStatus.AWAIT_CONFIRM.value, confirm_token=token)

    api_client.post(f"/api/v1/tasks/{task_id}/confirm", json={"confirm_token": token})

    with file_db.session() as session:
        events = list_events(session, task_id)
    assert events[-1].type == EventType.CONFIRMED.value
    assert events[-1].payload["actor"].startswith("human")


def test_task_status_column_untouched_by_events(file_db: Database) -> None:
    """事件是旁路：写事件不能改任务状态。"""
    task_id = seed_task(file_db)
    with file_db.session() as session:
        emit(session, task_id, EventType.QUEUED, {})
    with file_db.session() as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.status == TaskStatus.PENDING.value
