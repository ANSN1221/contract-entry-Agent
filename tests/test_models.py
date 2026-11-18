"""模型层验收：默认值、唯一约束、字段清单。"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import CONTRACT_FIELD_KEYS, CONTRACT_FIELDS, Task, TaskField, TaskStatus
from tests.conftest import make_task


def test_task_defaults(db) -> None:  # noqa: ANN001
    with db.session() as session:
        task = make_task()
        session.add(task)
        session.flush()

        assert task.status == TaskStatus.PENDING.value
        assert task.status_enum is TaskStatus.PENDING
        assert task.is_terminal is False
        assert task.version == 0
        assert task.attempt == 0
        assert task.round_count == 0
        assert len(task.id) == 32
        assert task.created_at.tzinfo is not None


def test_idempotency_key_is_unique(db) -> None:  # noqa: ANN001
    with db.session() as session:
        session.add(make_task(idempotency_key="key-1"))
        session.flush()

    with pytest.raises(IntegrityError):
        with db.session() as session:
            session.add(make_task(idempotency_key="key-1"))
            session.flush()


def test_duplicate_without_idempotency_key_allowed(db) -> None:  # noqa: ANN001
    """没带幂等键的任务可以有多条：NULL 不参与唯一约束冲突。"""
    with db.session() as session:
        session.add(make_task())
        session.add(make_task())
        session.flush()
        assert len(session.exec(select(Task)).all()) == 2


def test_confirm_token_is_unique(db) -> None:  # noqa: ANN001
    with db.session() as session:
        session.add(make_task(confirm_token="tok-1"))
        session.flush()

    with pytest.raises(IntegrityError):
        with db.session() as session:
            session.add(make_task(confirm_token="tok-1"))
            session.flush()


def test_task_field_unique_per_task_and_key(db) -> None:  # noqa: ANN001
    with db.session() as session:
        task = make_task()
        session.add(task)
        session.flush()
        session.add(TaskField(task_id=task.id, field_key="total_amount", value="100"))
        session.flush()

    with pytest.raises(IntegrityError):
        with db.session() as session:
            session.add(TaskField(task_id=task.id, field_key="total_amount", value="200"))
            session.flush()


def test_task_field_defaults(db) -> None:  # noqa: ANN001
    with db.session() as session:
        task = make_task()
        session.add(task)
        session.flush()
        field = TaskField(task_id=task.id, field_key="party_a", value="甲方公司")
        session.add(field)
        session.flush()

        assert field.confidence == 0.0
        assert field.needs_review is False
        assert field.verified is None


def test_payload_json_roundtrip(db) -> None:  # noqa: ANN001
    with db.session() as session:
        task = make_task()
        session.add(task)
        session.flush()
        payload = {"raw": "原文片段", "tokens": 12}
        session.add(TaskField(task_id=task.id, field_key="party_b", payload=payload))

    with Session(db.engine) as session:
        stored = session.exec(select(TaskField)).one()
        assert stored.payload == payload


def test_contract_fields_are_twelve_and_unique() -> None:
    assert len(CONTRACT_FIELDS) == 12
    assert len(CONTRACT_FIELD_KEYS) == 12
    assert "total_amount" in CONTRACT_FIELD_KEYS
    assert "dispute_resolution" in CONTRACT_FIELD_KEYS


def test_is_terminal_property() -> None:
    for status in (TaskStatus.DONE, TaskStatus.BLOCKED, TaskStatus.FAILED):
        assert Task(status=status.value).is_terminal is True
    for status in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.AWAIT_CONFIRM):
        assert Task(status=status.value).is_terminal is False
