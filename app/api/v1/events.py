"""任务事件流（SSE）。

三个细节决定它能不能用在真实浏览器里：

1. **`Last-Event-ID` 优先于查询参数**：浏览器 `EventSource` 重连时自动带上这个头，
   我们据此补发断线期间的事件，前端不会丢帧。
2. **终态即收尾**：任务到 done/failed 且事件读完就主动结束流，避免前端挂一个
   永远不会再有数据的连接。
3. **心跳帧**：每 15 秒发一个注释帧，防止中间的代理把空闲连接掐掉。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from app.errors import NotFound
from app.events import heartbeat, list_events, to_sse
from app.log import get_logger
from app.models import Task, TaskStatus
from app.store import get_database

log = get_logger("app.sse")

router = APIRouter(prefix="/tasks", tags=["events"])

POLL_INTERVAL = 0.5
HEARTBEAT_EVERY = 15.0
TERMINAL = frozenset({TaskStatus.DONE.value, TaskStatus.BLOCKED.value, TaskStatus.FAILED.value})


@router.get(
    "/{task_id}/events",
    summary="任务事件流（SSE）",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def stream_events(
    task_id: str,
    request: Request,
    follow: bool = Query(True, description="false 则只回放已有事件后立即结束"),
    since: int = Query(0, ge=0, description="从该 id 之后开始（Last-Event-ID 优先）"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    database = get_database()
    with database.session() as session:
        if session.get(Task, task_id) is None:
            raise NotFound("任务不存在", detail={"task_id": task_id})

    cursor = since
    if last_event_id and last_event_id.isdigit():
        cursor = int(last_event_id)

    return StreamingResponse(
        _iter_events(task_id, cursor, follow=follow, request=request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx 下必须关缓冲，否则事件会攒着一起发
        },
    )


async def _iter_events(
    task_id: str,
    cursor: int,
    *,
    follow: bool,
    request: Request,
) -> AsyncIterator[str]:
    database = get_database()
    idle = 0.0

    while True:
        if await request.is_disconnected():
            log.info("sse_client_disconnected", task_id=task_id, cursor=cursor)
            return

        with database.session() as session:
            events = list_events(session, task_id, after_id=cursor)
            task = session.get(Task, task_id)
            status = task.status if task else None

        for event in events:
            if event.id is not None:
                cursor = event.id
            yield to_sse(event)

        if not follow:
            return

        if status in TERMINAL:
            # 终态再补发一次，确保最后一条事件已经送出去
            with database.session() as session:
                tail = list_events(session, task_id, after_id=cursor)
            for event in tail:
                if event.id is not None:
                    cursor = event.id
                yield to_sse(event)
            log.info("sse_stream_closed", task_id=task_id, status=status)
            return

        idle += POLL_INTERVAL
        if idle >= HEARTBEAT_EVERY:
            idle = 0.0
            yield heartbeat()

        await asyncio.sleep(POLL_INTERVAL)
