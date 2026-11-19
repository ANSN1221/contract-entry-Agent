"""领域模型：任务、状态迁移审计、抽取字段。

两条约束刻在这里：

1. **状态存字符串而不是数据库枚举**
   SQLite 与 PostgreSQL 的枚举行为不一致，迁移成本高、排查时也不好读。
   状态合法性由 `state_machine.ALLOWED_TRANSITIONS` 保证，数据库只负责存。
2. **乐观锁放在 Task.version**
   并发迁移时用 `WHERE version = ?` 做 CAS，抢不到的那一方拿到 0 行影响，
   由状态机抛 `ConcurrentModification`。不依赖行锁，SQLite 上同样成立。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlmodel import Field, SQLModel


class TaskStatus(StrEnum):
    """任务状态。值即数据库里存的字符串，改值等于改数据，别随手改。"""

    PENDING = "pending"
    RUNNING = "running"
    AWAIT_CONFIRM = "await_confirm"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"


#: 12 个通用合同字段（计划书附录 A）。键名进数据库，标签进前端。
CONTRACT_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("contract_no", "合同编号", "string"),
    ("party_a", "甲方名称", "string"),
    ("party_b", "乙方名称", "string"),
    ("total_amount", "合同总价", "decimal"),
    ("signed_date", "签订日期", "date"),
    ("effective_date", "生效日期", "date"),
    ("performance_period", "履行期限", "string"),
    ("payment_terms", "付款方式", "text"),
    ("breach_liability", "违约责任", "text"),
    ("dispute_resolution", "争议解决方式", "string"),
    ("deposit_ratio", "履约保证金比例", "decimal"),
    ("warranty_period", "质保期", "string"),
)

CONTRACT_FIELD_KEYS: frozenset[str] = frozenset(key for key, _, _ in CONTRACT_FIELDS)

TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.DONE, TaskStatus.BLOCKED, TaskStatus.FAILED}
)


def utcnow() -> datetime:
    """统一用带时区的 UTC 入库，展示层再按本地时区换算。"""
    return datetime.now(UTC)


def new_id() -> str:
    return uuid4().hex


def _ts(index: bool = False) -> Any:
    return Column(DateTime(timezone=True), nullable=False, index=index)


class Task(SQLModel, table=True):
    __tablename__ = "task"

    id: str = Field(default_factory=new_id, sa_column=Column(String(32), primary_key=True))

    status: str = Field(
        default=TaskStatus.PENDING.value,
        sa_column=Column(String(20), nullable=False, index=True),
    )

    # 幂等：上传时客户端给的 Idempotency-Key，唯一约束挡住重复建任务
    idempotency_key: str | None = Field(
        default=None, sa_column=Column(String(80), nullable=True, unique=True)
    )

    # 确认卡凭证：一次性，用掉后 confirm_token_used_at 落时间戳
    confirm_token: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True, unique=True)
    )
    confirm_token_used_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    original_filename: str = Field(sa_column=Column(String(255), nullable=False))
    file_sha256: str = Field(sa_column=Column(String(64), nullable=False, index=True))
    file_path: str = Field(sa_column=Column(String(512), nullable=False))

    # 乐观锁版本号，每次状态迁移 +1
    version: int = Field(default=0, sa_column=Column(Integer, nullable=False, default=0))

    # 执行统计：轮次与重试次数，供 /metrics 用
    round_count: int = Field(default=0, sa_column=Column(Integer, nullable=False, default=0))
    attempt: int = Field(default=0, sa_column=Column(Integer, nullable=False, default=0))

    error_code: str | None = Field(default=None, sa_column=Column(String(40), nullable=True))
    blocked_reason: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    created_at: datetime = Field(default_factory=utcnow, sa_column=_ts(index=True))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=_ts())

    @property
    def status_enum(self) -> TaskStatus:
        return TaskStatus(self.status)

    @property
    def is_terminal(self) -> bool:
        return self.status_enum in TERMINAL_STATUSES


class TaskTransition(SQLModel, table=True):
    """迁移即日志：谁、从哪到哪、为什么、什么时候。这条链是状态机的审计凭据。"""

    __tablename__ = "task_transition"

    id: int | None = Field(default=None, primary_key=True)
    task_id: str = Field(sa_column=Column(String(32), nullable=False, index=True))
    from_status: str = Field(sa_column=Column(String(20), nullable=False))
    to_status: str = Field(sa_column=Column(String(20), nullable=False))
    reason: str | None = Field(default=None, sa_column=Column(String(200), nullable=True))
    actor: str = Field(default="system", sa_column=Column(String(40), nullable=False))
    request_id: str | None = Field(default=None, sa_column=Column(String(64), nullable=True))
    created_at: datetime = Field(default_factory=utcnow, sa_column=_ts(index=True))

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<TaskTransition {self.from_status}->{self.to_status} {self.reason or ''}>"


class TaskField(SQLModel, table=True):
    """单个字段的抽取结果。低置信字段靠 needs_review 标红，等人确认。"""

    __tablename__ = "task_field"
    __table_args__ = (UniqueConstraint("task_id", "field_key", name="uq_task_field_key"),)

    id: int | None = Field(default=None, primary_key=True)
    task_id: str = Field(sa_column=Column(String(32), nullable=False, index=True))
    field_key: str = Field(sa_column=Column(String(40), nullable=False))

    value: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    confidence: float = Field(default=0.0, sa_column=Column(Float, nullable=False, default=0.0))
    needs_review: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, default=False)
    )

    # 证据：这一字段是从哪条条款、哪一页抠出来的
    source_clause_id: str | None = Field(default=None, sa_column=Column(String(64), nullable=True))
    source_page: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    evidence: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    # 引用校验结论：null 表示还没校验
    verified: bool | None = Field(default=None, sa_column=Column(Boolean, nullable=True))
    verify_note: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    payload: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))

    created_at: datetime = Field(default_factory=utcnow, sa_column=_ts())
    updated_at: datetime = Field(default_factory=utcnow, sa_column=_ts())


class Event(SQLModel, table=True):
    """任务事件流。自增 id 就是 SSE 的 `Last-Event-ID`，断线后靠它补发。

    为什么不复用 TaskTransition：状态迁移只是事件的一种。检索完成、抽取完成、
    校验结论这些不进状态机，前端时间线却要显示，所以单独一张表。
    """

    __tablename__ = "event"

    id: int | None = Field(default=None, primary_key=True)
    task_id: str = Field(sa_column=Column(String(32), nullable=False, index=True))
    type: str = Field(sa_column=Column(String(40), nullable=False, index=True))
    payload: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    created_at: datetime = Field(default_factory=utcnow, sa_column=_ts(index=True))
