"""事件流：写入事件、按游标读取、序列化成 SSE 帧。

事件与状态迁移分开存：迁移是状态机的账，事件是给前端看的叙事。
检索完成、抽取完成这类「过程事实」不进状态机，但时间线要显示。

SSE 帧格式（`id` 就是自增主键，客户端断线重连带回来）：

    id: 12
    event: extraction.done
    data: {"filled": 11}

    （空行分隔）
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from sqlmodel import Session, select

from app.log import get_logger
from app.models import Event

log = get_logger("app.events")


class EventType(StrEnum):
    QUEUED = "task.queued"
    RUNNING = "task.running"
    RETRIEVED = "retrieval.done"
    EXTRACTED = "extraction.done"
    VERIFIED = "verification.done"
    RETRY = "extraction.retry"
    AWAIT_CONFIRM = "task.await_confirm"
    CONFIRMED = "task.confirmed"
    DONE = "task.done"
    FAILED = "task.failed"


def emit(
    session: Session,
    task_id: str,
    event_type: EventType | str,
    payload: dict[str, Any] | None = None,
) -> Event:
    """写一条事件。调用方负责 commit——和状态迁移共用同一事务。"""
    event = Event(
        task_id=task_id,
        type=str(event_type),
        payload=payload or {},
    )
    session.add(event)
    session.flush()
    log.debug("event_emitted", task_id=task_id, type=str(event_type), event_id=event.id)
    return event


def list_events(
    session: Session,
    task_id: str,
    *,
    after_id: int = 0,
    limit: int = 200,
) -> list[Event]:
    stmt = (
        select(Event)
        .where(Event.task_id == task_id, Event.id > after_id)
        .order_by(Event.id)
        .limit(limit)
    )
    return list(session.exec(stmt))


def to_sse(event: Event) -> str:
    """一条事件 → 一个 SSE 帧。data 必须单行，JSON 里的换行会被转义。"""
    data = json.dumps(
        {
            "task_id": event.task_id,
            "type": event.type,
            "payload": event.payload or {},
            "created_at": event.created_at.isoformat(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {event.id}\nevent: {event.type}\ndata: {data}\n\n"


def heartbeat() -> str:
    """注释帧：只为不让中间代理掐断连接，客户端会忽略。"""
    return ": ping\n\n"
