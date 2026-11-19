"""执行器验收：状态流转、字段落库、超时、异常、死信与并发保护。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlmodel import select

from app.config import Settings
from app.errors import Conflict, NotFound
from app.models import Task, TaskStatus, TaskTransition
from app.runner import TaskRunner, load_fields
from app.store import Database
from tests.conftest import make_task


class FakeGraph:
    """替身图：直接返回预设结果，或按需抛异常/拖延。"""

    def __init__(
        self,
        result: dict[str, Any] | None = None,
        *,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.result = result or {}
        self.error = error
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    async def ainvoke(self, state: dict[str, Any], config: dict | None = None) -> dict:
        self.calls.append({"state": state, "config": config})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result


def make_result(outcome: str = "await_confirm", **overrides) -> dict:
    result = {
        "outcome": outcome,
        "rounds": 1,
        "review_keys": ["deposit_ratio"],
        "unsupported": [],
        "verified": {"deposit_ratio": "unverified"},
        "tokens": {"prompt": 100, "completion": 50},
        "error": "",
        "drafts": [
            {
                "field_key": "total_amount",
                "value": "1280000.00",
                "confidence": 0.95,
                "clause_id": "DOC901-C0003",
                "quote": "¥1,280,000.00",
                "needs_review": False,
            },
            {
                "field_key": "deposit_ratio",
                "value": "5",
                "confidence": 0.4,
                "clause_id": "DOC901-C0008",
                "quote": "百分之五",
                "needs_review": True,
            },
        ],
    }
    result.update(overrides)
    return result


def make_runner(db: Database, graph: FakeGraph, **overrides) -> TaskRunner:
    settings = Settings(
        _env_file=None,
        env="test",
        runner_workers=0,
        runner_timeout_seconds=overrides.pop("timeout", 2.0),
        **overrides,
    )
    return TaskRunner(database=db, graph=graph, settings=settings)


def create_task(db: Database) -> str:
    with db.session() as session:
        task = make_task()
        session.add(task)
        session.flush()
        return task.id


def get_task(db: Database, task_id: str) -> Task:
    with db.session() as session:
        task = session.get(Task, task_id)
        assert task is not None
        return task


def transitions(db: Database, task_id: str) -> list[str]:
    with db.session() as session:
        stmt = (
            select(TaskTransition)
            .where(TaskTransition.task_id == task_id)
            .order_by(TaskTransition.id)
        )
        return [f"{r.from_status}->{r.to_status}" for r in session.exec(stmt)]


# ---------- 正常路径 ----------


async def test_run_pending_task_to_await_confirm(db: Database) -> None:
    graph = FakeGraph(make_result("await_confirm"))
    runner = make_runner(db, graph)
    task_id = create_task(db)

    outcome = await runner.run_task(task_id)

    assert outcome.status == TaskStatus.AWAIT_CONFIRM.value
    assert outcome.review_keys == ["deposit_ratio"]
    assert outcome.tokens["prompt"] == 100
    assert transitions(db, task_id) == ["pending->running", "running->await_confirm"]
    assert get_task(db, task_id).round_count == 1


async def test_run_persists_fields(db: Database) -> None:
    runner = make_runner(db, FakeGraph(make_result()))
    task_id = create_task(db)

    await runner.run_task(task_id)

    with db.session() as session:
        fields = {f.field_key: f for f in load_fields(session, task_id)}
    assert set(fields) == {"total_amount", "deposit_ratio"}
    assert fields["total_amount"].value == "1280000.00"
    assert fields["total_amount"].verified is None  # 没出现在 verified 里
    assert fields["deposit_ratio"].needs_review is True
    assert fields["deposit_ratio"].verify_note == "unverified"
    assert fields["deposit_ratio"].evidence == "百分之五"


async def test_high_confidence_goes_done(db: Database) -> None:
    runner = make_runner(db, FakeGraph(make_result("done", review_keys=[], verified={})))
    task_id = create_task(db)

    outcome = await runner.run_task(task_id)

    assert outcome.status == TaskStatus.DONE.value
    assert get_task(db, task_id).is_terminal is True


async def test_task_id_passed_as_thread_id(db: Database) -> None:
    graph = FakeGraph(make_result())
    runner = make_runner(db, graph)
    task_id = create_task(db)

    await runner.run_task(task_id)

    assert graph.calls[0]["config"]["configurable"]["thread_id"] == task_id


# ---------- 失败与死信 ----------


async def test_graph_exception_marks_failed(db: Database) -> None:
    runner = make_runner(db, FakeGraph(error=RuntimeError("模型炸了")))
    task_id = create_task(db)

    outcome = await runner.run_task(task_id)

    assert outcome.status == TaskStatus.FAILED.value
    task = get_task(db, task_id)
    assert task.error_code == "RUNNER_ERROR"
    assert "RuntimeError" in (task.blocked_reason or "")
    assert transitions(db, task_id) == ["pending->running", "running->failed"]


async def test_timeout_marks_failed(db: Database) -> None:
    runner = make_runner(db, FakeGraph(make_result(), delay=1.0), timeout=0.05)
    task_id = create_task(db)

    outcome = await runner.run_task(task_id)

    assert outcome.status == TaskStatus.FAILED.value
    assert get_task(db, task_id).error_code == "TIMEOUT"


async def test_empty_extraction_marks_failed(db: Database) -> None:
    runner = make_runner(db, FakeGraph(make_result("failed", drafts=[])))
    task_id = create_task(db)

    outcome = await runner.run_task(task_id)

    assert outcome.status == TaskStatus.FAILED.value
    assert get_task(db, task_id).error_code == "EXTRACT_EMPTY"


async def test_blocked_reason_records_unsupported(db: Database) -> None:
    result = make_result(unsupported=["breach_liability", "party_a"])
    runner = make_runner(db, FakeGraph(result))
    task_id = create_task(db)

    await runner.run_task(task_id)

    assert "breach_liability" in (get_task(db, task_id).blocked_reason or "")


# ---------- 入口守卫 ----------


async def test_missing_task_raises_not_found(db: Database) -> None:
    runner = make_runner(db, FakeGraph(make_result()))
    with pytest.raises(NotFound):
        await runner.run_task("no-such-task")


async def test_terminal_task_rejected(db: Database) -> None:
    runner = make_runner(db, FakeGraph(make_result()))
    task_id = create_task(db)
    with db.session() as session:
        task = session.get(Task, task_id)
        assert task is not None
        task.status = TaskStatus.DONE.value
        session.add(task)

    with pytest.raises(Conflict) as exc:
        await runner.run_task(task_id)
    assert exc.value.code == "TASK_TERMINAL"


async def test_already_running_rejected_without_resume(db: Database) -> None:
    runner = make_runner(db, FakeGraph(make_result()))
    task_id = create_task(db)
    with db.session() as session:
        task = session.get(Task, task_id)
        assert task is not None
        task.status = TaskStatus.RUNNING.value
        session.add(task)

    with pytest.raises(Conflict) as exc:
        await runner.run_task(task_id)
    assert exc.value.code == "ALREADY_RUNNING"


async def test_resume_running_task_allowed(db: Database) -> None:
    graph = FakeGraph(make_result())
    runner = make_runner(db, graph)
    task_id = create_task(db)
    with db.session() as session:
        task = session.get(Task, task_id)
        assert task is not None
        task.status = TaskStatus.RUNNING.value
        session.add(task)

    outcome = await runner.run_task(task_id, resume=True)

    assert outcome.status == TaskStatus.AWAIT_CONFIRM.value
    # 续跑只传 task_id，让 checkpointer 自己恢复中间状态
    assert graph.calls[0]["state"] == {"task_id": task_id}


async def test_attempt_counter_increments(db: Database) -> None:
    runner = make_runner(db, FakeGraph(make_result("done", review_keys=[], verified={})))
    task_id = create_task(db)

    await runner.run_task(task_id)

    assert get_task(db, task_id).attempt == 1


# ---------- 死信 ----------


async def test_dead_letter_after_max_attempts(db: Database) -> None:
    """反复失败的任务不再重试，转终态并留证据。"""
    graph = FakeGraph(make_result("done", review_keys=[], verified={}))
    runner = make_runner(db, graph, max_attempts=2)
    task_id = create_task(db)
    with db.session() as session:
        task = session.get(Task, task_id)
        assert task is not None
        task.attempt = 2  # 已经跑过两次
        session.add(task)

    outcome = await runner.run_task(task_id)

    assert outcome.status == TaskStatus.FAILED.value
    assert "死信" in outcome.error
    task = get_task(db, task_id)
    assert task.error_code == "DEAD_LETTER"
    assert graph.calls == []  # 没有再执行图
    assert transitions(db, task_id)[-1] == "running->failed"


async def test_dead_letter_emits_event(db: Database) -> None:
    from app.events import list_events

    runner = make_runner(db, FakeGraph(make_result()), max_attempts=1)
    task_id = create_task(db)
    with db.session() as session:
        task = session.get(Task, task_id)
        assert task is not None
        task.attempt = 1
        session.add(task)

    await runner.run_task(task_id)

    with db.session() as session:
        types = [e.type for e in list_events(session, task_id)]
    assert "task.failed" in types


async def test_attempts_below_limit_still_run(db: Database) -> None:
    graph = FakeGraph(make_result("await_confirm"))
    runner = make_runner(db, graph, max_attempts=3)
    task_id = create_task(db)
    with db.session() as session:
        task = session.get(Task, task_id)
        assert task is not None
        task.attempt = 1
        session.add(task)

    outcome = await runner.run_task(task_id)

    assert outcome.status == TaskStatus.AWAIT_CONFIRM.value
    assert get_task(db, task_id).attempt == 2
