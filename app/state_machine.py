"""状态机：迁移只能走白名单，非法迁移直接报错，每次迁移落一条审计。

为什么不用 if/else 散落各处：
状态判断一旦写在业务里，就会出现「这个接口允许、那个接口忘了拦」的漏洞。
把它收敛成一张表，新增接口自动受同一套规则约束，改规则只改一处。

白名单刻意取最小集：

    pending       → running                      排队后被调度器领走
    running       → await_confirm                抽取完成，有低置信字段等人确认
    running       → done                         无可疑字段，直接写库完成
    running       → blocked                      缺关键信息，等人补充
    running       → failed                       重试用尽 / 不可恢复错误
    await_confirm → done                         人工确认后写库
    await_confirm → running                      用户要求重新抽取
    await_confirm → blocked                      确认超时，转人工

终态 done / blocked / failed 是吸收态，出边的集合为空。任何针对终态的操作
（确认、重试、重复提交）在业务层应返回 409，而不是偷偷改回去。
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import update
from sqlmodel import Session, select

from app.models import Task, TaskStatus, TaskTransition, utcnow


class InvalidTransition(Exception):
    """迁移不在白名单里：要么状态写错，要么调用方逻辑有 bug。"""

    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(f"不允许的状态迁移：{current} → {target}")


class ConcurrentModification(Exception):
    """乐观锁冲突：这个任务在你读到它之后已经被别人改过。"""

    def __init__(self, task_id: str, expected_version: int) -> None:
        self.task_id = task_id
        self.expected_version = expected_version
        super().__init__(
            f"任务 {task_id} 在 version={expected_version} 时已被并发修改，请重新读取后重试"
        )


#: 白名单。写成 frozenset 是为了让「有没有这条边」变成 O(1) 判断，
#: 也顺便防止运行时被误改。
ALLOWED_TRANSITIONS: Final[dict[TaskStatus, frozenset[TaskStatus]]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.RUNNING}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.AWAIT_CONFIRM,
            TaskStatus.DONE,
            TaskStatus.BLOCKED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.AWAIT_CONFIRM: frozenset({TaskStatus.DONE, TaskStatus.RUNNING, TaskStatus.BLOCKED}),
    TaskStatus.DONE: frozenset(),
    TaskStatus.BLOCKED: frozenset(),
    TaskStatus.FAILED: frozenset(),
}


def parse_status(value: TaskStatus | str) -> TaskStatus:
    if isinstance(value, TaskStatus):
        return value
    try:
        return TaskStatus(value)
    except ValueError as exc:
        raise InvalidTransition(str(value), "unknown") from exc


def allowed_targets(current: TaskStatus | str) -> frozenset[TaskStatus]:
    return ALLOWED_TRANSITIONS[parse_status(current)]


def can_transition(current: TaskStatus | str, target: TaskStatus | str) -> bool:
    return parse_status(target) in allowed_targets(current)


def is_terminal(status: TaskStatus | str) -> bool:
    return not allowed_targets(status)


def assert_transition(current: TaskStatus | str, target: TaskStatus | str) -> TaskStatus:
    """白名单校验。通过返回目标状态，不通过抛 InvalidTransition。"""
    cur = parse_status(current)
    tgt = parse_status(target)
    if tgt not in ALLOWED_TRANSITIONS[cur]:
        raise InvalidTransition(cur.value, tgt.value)
    return tgt


def transition(
    session: Session,
    task: Task,
    target: TaskStatus | str,
    *,
    reason: str | None = None,
    actor: str = "system",
    request_id: str | None = None,
) -> TaskTransition:
    """执行一次状态迁移：白名单校验 → 乐观锁更新 → 落审计记录。

    调用方负责 commit。抛异常时不留半条记录：审计与状态更新在同一事务里。
    """
    tgt = assert_transition(task.status, target)

    session.flush()  # 新建的任务还没 insert 时，后面的 UPDATE 会打空

    now = utcnow()
    result = session.execute(
        update(Task)
        .where(Task.id == task.id, Task.version == task.version)
        .values(status=tgt.value, version=Task.version + 1, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise ConcurrentModification(task.id, task.version)

    record = TaskTransition(
        task_id=task.id,
        from_status=task.status,
        to_status=tgt.value,
        reason=reason,
        actor=actor,
        request_id=request_id,
    )
    session.add(record)

    # ORM 里的对象还拿着旧值，刷新一下，避免 commit 时把旧状态写回覆盖
    session.refresh(task)
    return record


def list_transitions(session: Session, task_id: str) -> list[TaskTransition]:
    """按时间正序返回审计链，前端时间线直接用它。"""
    stmt = (
        select(TaskTransition).where(TaskTransition.task_id == task_id).order_by(TaskTransition.id)
    )
    return list(session.exec(stmt))
