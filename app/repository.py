"""仓储层：任务的数据库操作收敛在这里，接口层不直接写 SQL。

约定：
- 方法全部收 Session 参数，事务边界由调用方（FastAPI 依赖）负责；
- 业务失败翻译成 AppError，HTTP 层不接触 SQLAlchemy 异常；
- 状态迁移复用 `state_machine.transition`，这里只做「找到任务 + 异常翻译」。
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.errors import Conflict, NotFound
from app.models import Task, TaskStatus, TaskTransition
from app.state_machine import (
    ConcurrentModification,
    InvalidTransition,
)
from app.state_machine import (
    list_transitions as query_transitions,
)
from app.state_machine import (
    transition as apply_transition,
)


class TaskRepository:
    """任务仓储。无状态，一个进程一个实例就够。"""

    def create(
        self,
        session: Session,
        *,
        payload: dict,
        idempotency_key: str | None = None,
    ) -> tuple[Task, bool]:
        """建任务。带幂等键时重复提交返回已存在任务，`created=False`。"""
        if idempotency_key:
            existing = self._get_by_idempotency_key(session, idempotency_key)
            if existing:
                return existing, False

        task = Task(idempotency_key=idempotency_key, **payload)
        session.add(task)
        try:
            session.flush()
        except IntegrityError:
            # 并发下两条同键请求同时插入：唯一约束兜底，回滚后取已提交的那条
            session.rollback()
            existing = self._get_by_idempotency_key(session, idempotency_key)
            if existing:
                return existing, False
            raise
        return task, True

    def get(self, session: Session, task_id: str) -> Task | None:
        return session.get(Task, task_id)

    def list(
        self,
        session: Session,
        *,
        offset: int,
        limit: int,
        status: TaskStatus | None = None,
    ) -> tuple[list[Task], int]:
        """按创建时间倒序返回一页任务和总条数。"""
        stmt = select(Task)
        count_stmt = select(func.count()).select_from(Task)
        if status is not None:
            stmt = stmt.where(Task.status == status.value)
            count_stmt = count_stmt.where(Task.status == status.value)

        total = session.exec(count_stmt).one()
        rows = session.exec(
            stmt.order_by(Task.created_at.desc(), Task.id.desc()).offset(offset).limit(limit)
        )
        return list(rows), total

    def list_transitions(self, session: Session, task_id: str) -> list[TaskTransition]:
        return query_transitions(session, task_id)

    def transition(
        self,
        session: Session,
        *,
        task_id: str,
        target: TaskStatus | str,
        version: int,
        reason: str | None = None,
        actor: str = "system",
        request_id: str | None = None,
    ) -> Task:
        """按版本号做一次状态迁移，白名单与 CAS 都在状态机里。"""
        task = self.get(session, task_id)
        if task is None:
            raise NotFound("任务不存在", detail={"task_id": task_id})
        if task.version != version:
            raise Conflict(
                code="CONCURRENT_MODIFICATION",
                message="任务已被并发修改，请重新读取后重试",
                detail={
                    "task_id": task_id,
                    "expected_version": version,
                    "actual_version": task.version,
                },
            )
        try:
            apply_transition(
                session,
                task,
                target,
                reason=reason,
                actor=actor,
                request_id=request_id,
            )
        except InvalidTransition as exc:
            raise Conflict(
                code="INVALID_TRANSITION",
                message=f"不允许的状态迁移：{exc.current} → {exc.target}",
                detail={"from": exc.current, "to": exc.target},
            ) from exc
        except ConcurrentModification as exc:
            raise Conflict(
                code="CONCURRENT_MODIFICATION",
                message="任务已被并发修改，请重新读取后重试",
                detail={"task_id": exc.task_id, "expected_version": exc.expected_version},
            ) from exc
        return task

    def _get_by_idempotency_key(self, session: Session, key: str) -> Task | None:
        stmt = select(Task).where(Task.idempotency_key == key)
        return session.exec(stmt).first()


repository = TaskRepository()
