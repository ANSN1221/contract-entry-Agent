"""仓储层：任务的数据库操作收敛在这里，接口层不直接写 SQL。

约定：
- 方法全部收 Session 参数，事务边界由调用方（FastAPI 依赖）负责；
- 业务失败翻译成 AppError，HTTP 层不接触 SQLAlchemy 异常；
- 状态迁移复用 `state_machine.transition`，这里只做「找到任务 + 异常翻译」。
"""

from __future__ import annotations

import secrets

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.errors import BadRequest, Conflict, Forbidden, NotFound
from app.events import EventType, emit
from app.models import CONTRACT_FIELD_KEYS, Task, TaskField, TaskStatus, TaskTransition, utcnow
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

    def list_fields(self, session: Session, task_id: str) -> list[TaskField]:
        stmt = select(TaskField).where(TaskField.task_id == task_id).order_by(TaskField.field_key)
        return list(session.exec(stmt))

    def confirm(
        self,
        session: Session,
        *,
        task_id: str,
        confirm_token: str,
        updates: list[tuple[str, str | None]],
        actor: str,
        version: int | None = None,
        request_id: str | None = None,
    ) -> Task:
        """人工确认：校验一次性 token → 应用人工修改 → 转 done。

        四道闸门，顺序按「越便宜越先」：任务存在 → 状态对 → token 对且没用过 → 版本对。
        """
        task = self.get(session, task_id)
        if task is None:
            raise NotFound("任务不存在", detail={"task_id": task_id})

        if task.status != TaskStatus.AWAIT_CONFIRM.value:
            raise Conflict(
                code="TASK_NOT_AWAIT_CONFIRM",
                message=f"任务当前状态为 {task.status}，不能确认",
                detail={"task_id": task_id, "status": task.status},
            )

        if task.confirm_token_used_at is not None:
            raise Conflict(
                code="CONFIRM_ALREADY_USED",
                message="确认凭证已被使用",
                detail={"task_id": task_id, "used_at": task.confirm_token_used_at.isoformat()},
            )

        # 常量时间比较，避免按字节猜 token
        if not task.confirm_token or not secrets.compare_digest(confirm_token, task.confirm_token):
            raise Forbidden("确认凭证无效", detail={"task_id": task_id})

        if version is not None and task.version != version:
            raise Conflict(
                code="CONCURRENT_MODIFICATION",
                message="任务已被并发修改，请重新读取后重试",
                detail={"expected_version": version, "actual_version": task.version},
            )

        self._apply_updates(session, task_id, updates)

        task.confirm_token_used_at = utcnow()
        task.updated_at = utcnow()
        if task.blocked_reason and task.blocked_reason.startswith("字段校验未通过"):
            task.blocked_reason = None
        session.add(task)
        session.flush()

        try:
            apply_transition(
                session,
                task,
                TaskStatus.DONE,
                reason="人工确认后写库",
                actor=actor,
                request_id=request_id,
            )
        except InvalidTransition as exc:  # 理论上到不了，白名单与状态检查重复兜底
            raise Conflict(
                code="INVALID_TRANSITION",
                message=f"不允许的状态迁移：{exc.current} → {exc.target}",
            ) from exc

        emit(
            session,
            task_id,
            EventType.CONFIRMED,
            {"actor": actor, "edited": [key for key, _ in updates]},
        )
        return task

    def _apply_updates(
        self,
        session: Session,
        task_id: str,
        updates: list[tuple[str, str | None]],
    ) -> None:
        if not updates:
            # 没改动也要把待确认标记清掉：人工点了确认，等于全部认可
            for field in self.list_fields(session, task_id):
                field.needs_review = False
                field.verified = True
                field.verify_note = "human_confirmed"
                field.updated_at = utcnow()
                session.add(field)
            return

        known = {field.field_key: field for field in self.list_fields(session, task_id)}
        for key, value in updates:
            if key not in CONTRACT_FIELD_KEYS:
                raise BadRequest(f"未知字段：{key}", detail={"field_key": key})
            field = known.get(key)
            if field is None:
                field = TaskField(task_id=task_id, field_key=key)
            field.value = value
            field.needs_review = False
            field.verified = True
            field.verify_note = "human_confirmed"
            field.updated_at = utcnow()
            session.add(field)

        # 没被修改的字段也同样视为已确认
        for key, field in known.items():
            if any(key == update_key for update_key, _ in updates):
                continue
            field.needs_review = False
            field.verified = True
            field.verify_note = "human_confirmed"
            session.add(field)
        session.flush()


repository = TaskRepository()
