"""任务 API：创建、幂等、查询、分页列表与状态迁移。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from sqlmodel import Session

from app.errors import NotFound
from app.models import Task, TaskStatus
from app.repository import repository
from app.schemas import TaskCreate, TaskDetail, TaskListOut, TaskOut, TransitionIn, TransitionOut
from app.store import get_session

router = APIRouter(prefix="/tasks", tags=["tasks"])

SessionDep = Annotated[Session, Depends(get_session)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=TaskOut, summary="创建任务")
def create_task(
    payload: TaskCreate,
    session: SessionDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=80)] = None,
) -> Task:
    task, created = repository.create(
        session, payload=payload.model_dump(), idempotency_key=idempotency_key
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return task


@router.get("", response_model=TaskListOut, summary="任务列表")
def list_tasks(
    session: SessionDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    status_filter: Annotated[TaskStatus | None, Query(alias="status")] = None,
) -> TaskListOut:
    items, total = repository.list(session, offset=offset, limit=limit, status=status_filter)
    return TaskListOut(items=items, total=total, offset=offset, limit=limit)


@router.get("/{task_id}", response_model=TaskDetail, summary="任务详情（含审计时间线）")
def get_task(task_id: str, session: SessionDep) -> TaskDetail:
    task = repository.get(session, task_id)
    if task is None:
        raise NotFound("任务不存在", detail={"task_id": task_id})
    records = repository.list_transitions(session, task_id)
    return TaskDetail(
        **task.model_dump(),
        transitions=[TransitionOut.model_validate(record) for record in records],
    )


@router.post("/{task_id}/transitions", response_model=TaskOut, summary="状态迁移")
def transition_task(
    task_id: str, payload: TransitionIn, session: SessionDep, request: Request
) -> Task:
    return repository.transition(
        session,
        task_id=task_id,
        target=payload.to_status,
        version=payload.version,
        reason=payload.reason,
        actor=payload.actor,
        request_id=getattr(request.state, "request_id", None),
    )
