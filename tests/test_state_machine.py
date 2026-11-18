"""状态机验收：白名单全覆盖、终态吸收、迁移即审计、乐观锁。

计划书验收标准写的是「100% 白名单，非法迁移有测试覆盖」——
所以非法组合不是挑两个举例，而是把 6×6 的组合全跑一遍。
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from app.models import Task, TaskStatus, TaskTransition
from app.state_machine import (
    ALLOWED_TRANSITIONS,
    ConcurrentModification,
    InvalidTransition,
    allowed_targets,
    assert_transition,
    can_transition,
    is_terminal,
    list_transitions,
    transition,
)
from app.store import Database
from tests.conftest import make_task

ALL_STATUSES = list(TaskStatus)

#: 计划书第五章写的边。改这张表等于改产品行为，必须是有意识的决定。
EXPECTED_EDGES = {
    (TaskStatus.PENDING, TaskStatus.RUNNING),
    (TaskStatus.RUNNING, TaskStatus.AWAIT_CONFIRM),
    (TaskStatus.RUNNING, TaskStatus.DONE),
    (TaskStatus.RUNNING, TaskStatus.BLOCKED),
    (TaskStatus.RUNNING, TaskStatus.FAILED),
    (TaskStatus.AWAIT_CONFIRM, TaskStatus.DONE),
    (TaskStatus.AWAIT_CONFIRM, TaskStatus.RUNNING),
    (TaskStatus.AWAIT_CONFIRM, TaskStatus.BLOCKED),
}

ILLEGAL_PAIRS = [
    (src, dst) for src in ALL_STATUSES for dst in ALL_STATUSES if (src, dst) not in EXPECTED_EDGES
]


def _create(session: Session, **overrides) -> Task:
    task = make_task(**overrides)
    session.add(task)
    session.flush()
    return task


# ---------- 白名单本身 ----------


def test_whitelist_matches_plan() -> None:
    actual = {(src, dst) for src, targets in ALLOWED_TRANSITIONS.items() for dst in targets}
    assert actual == EXPECTED_EDGES


@pytest.mark.parametrize(
    ("src", "dst"),
    sorted(EXPECTED_EDGES, key=lambda p: (p[0].value, p[1].value)),
)
def test_can_transition_true_for_whitelisted(src: TaskStatus, dst: TaskStatus) -> None:
    assert can_transition(src, dst) is True
    assert assert_transition(src, dst) is dst


@pytest.mark.parametrize(("src", "dst"), ILLEGAL_PAIRS)
def test_invalid_transition_rejected(src: TaskStatus, dst: TaskStatus) -> None:
    """28 种非法组合（含自环与终态出边）全部拒绝。"""
    assert can_transition(src, dst) is False
    with pytest.raises(InvalidTransition) as exc:
        assert_transition(src, dst)
    assert exc.value.current == src.value
    assert exc.value.target == dst.value


def test_terminal_states_have_no_exit() -> None:
    for status in (TaskStatus.DONE, TaskStatus.BLOCKED, TaskStatus.FAILED):
        assert allowed_targets(status) == frozenset()
        assert is_terminal(status) is True
    for status in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.AWAIT_CONFIRM):
        assert is_terminal(status) is False


def test_unknown_status_value_rejected() -> None:
    with pytest.raises(InvalidTransition):
        assert_transition("archived", TaskStatus.RUNNING)
    with pytest.raises(InvalidTransition):
        assert_transition(TaskStatus.PENDING, "flying")


# ---------- 真实迁移 ----------


def test_happy_path_pending_to_done(session: Session) -> None:
    task = _create(session)
    assert (task.status, task.version) == (TaskStatus.PENDING.value, 0)

    transition(session, task, TaskStatus.RUNNING, reason="调度器领取", actor="runner")
    assert (task.status, task.version) == (TaskStatus.RUNNING.value, 1)

    transition(session, task, TaskStatus.AWAIT_CONFIRM, reason="低置信字段", actor="graph")
    transition(session, task, TaskStatus.DONE, reason="人工确认", actor="human:alice")

    assert (task.status, task.version) == (TaskStatus.DONE.value, 3)


def test_transition_writes_audit_record(session: Session) -> None:
    task = _create(session)
    record = transition(
        session,
        task,
        TaskStatus.RUNNING,
        reason="调度器领取",
        actor="runner",
        request_id="req-123",
    )

    assert record.from_status == TaskStatus.PENDING.value
    assert record.to_status == TaskStatus.RUNNING.value
    assert record.reason == "调度器领取"
    assert record.actor == "runner"
    assert record.request_id == "req-123"
    assert record.created_at.tzinfo is not None


def test_audit_chain_is_ordered(session: Session) -> None:
    task = _create(session)
    transition(session, task, TaskStatus.RUNNING, reason="a")
    transition(session, task, TaskStatus.BLOCKED, reason="缺乙方名称")

    chain = list_transitions(session, task.id)
    assert [r.to_status for r in chain] == ["running", "blocked"]
    assert chain[0].id is not None and chain[0].id < chain[1].id


def test_terminal_task_cannot_move(session: Session) -> None:
    task = _create(session)
    transition(session, task, TaskStatus.RUNNING, reason="a")
    transition(session, task, TaskStatus.FAILED, reason="重试用尽")

    with pytest.raises(InvalidTransition):
        transition(session, task, TaskStatus.RUNNING, reason="偷偷复活")
    assert task.status == TaskStatus.FAILED.value


def test_failed_actor_defaults_to_system(session: Session) -> None:
    task = _create(session)
    record = transition(session, task, TaskStatus.RUNNING)
    assert record.actor == "system"


# ---------- 事务与并发 ----------


def test_rollback_leaves_no_audit_record(db: Database) -> None:
    """迁移和审计在同一事务：中途炸掉不允许留半条记录。"""
    with pytest.raises(RuntimeError):
        with db.session() as session:
            task = _create(session)
            transition(session, task, TaskStatus.RUNNING, reason="a")
            raise RuntimeError("模拟后续步骤失败")

    with db.session() as session:
        assert session.exec(select(TaskTransition)).all() == []


def test_concurrent_modification_raises(file_db: Database) -> None:
    """两个会话读到同一个 version，后写的那个必须失败，而不是覆盖。"""
    with file_db.session() as session:
        task = _create(session)
        task_id = task.id

    with file_db.session() as first:
        t1 = first.get(Task, task_id)
        assert t1 is not None and t1.version == 0

        with file_db.session() as second:
            t2 = second.get(Task, task_id)
            assert t2 is not None
            transition(second, t2, TaskStatus.RUNNING, reason="先到")

        with pytest.raises(ConcurrentModification):
            transition(first, t1, TaskStatus.RUNNING, reason="后到")


def test_version_guards_double_transition(file_db: Database) -> None:
    """同一会话里连做两次相同迁移，第二次必然非法（状态已变）。"""
    with file_db.session() as session:
        task = _create(session)
        transition(session, task, TaskStatus.RUNNING, reason="第一次")
        with pytest.raises(InvalidTransition):
            transition(session, task, TaskStatus.RUNNING, reason="第二次")
