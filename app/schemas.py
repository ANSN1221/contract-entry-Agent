"""接口契约：请求与响应的 pydantic 模型，与数据库模型分开演化。

数据库列改名不影响对外的 JSON 形状，反过来也一样。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import TaskStatus


class TaskCreate(BaseModel):
    original_filename: str = Field(min_length=1, max_length=255)
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_path: str = Field(min_length=1, max_length=512)


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    version: int
    original_filename: str
    file_sha256: str
    file_path: str
    round_count: int
    attempt: int
    error_code: str | None
    blocked_reason: str | None
    created_at: datetime
    updated_at: datetime


class TransitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: str
    from_status: str
    to_status: str
    reason: str | None
    actor: str
    request_id: str | None
    created_at: datetime


class TaskDetail(TaskOut):
    transitions: list[TransitionOut]
    fields: list[FieldOut] = Field(default_factory=list)
    # 只有在 await_confirm 时才有值，前端拿它提交确认卡
    confirm_token: str | None = None


class TaskListOut(BaseModel):
    items: list[TaskOut]
    total: int
    offset: int
    limit: int


class TransitionIn(BaseModel):
    to_status: TaskStatus
    version: int = Field(ge=0)
    reason: str | None = Field(default=None, max_length=200)
    actor: str = Field(default="system", min_length=1, max_length=40)


class FieldUpdate(BaseModel):
    """人工修正单个字段。值为 null 表示「确认这条确实没有」。"""

    field_key: str = Field(min_length=1, max_length=40)
    value: str | None = Field(default=None, max_length=2000)


class ConfirmIn(BaseModel):
    """确认卡提交。`fields` 是人改过的字段，只传改动的那些。"""

    confirm_token: str = Field(min_length=8, max_length=64)
    fields: list[FieldUpdate] = Field(default_factory=list)
    version: int | None = Field(default=None, ge=0, description="带上则做乐观锁校验")


class RunOut(BaseModel):
    """执行结果。`review_keys` 是需要在确认卡上标红的字段，`confirm_token` 用于提交确认。"""

    task_id: str
    status: str
    outcome: str = ""
    rounds: int = 0
    error: str = ""
    tokens: dict[str, int] = Field(default_factory=dict)
    review_keys: list[str] = Field(default_factory=list)
    unsupported: list[str] = Field(default_factory=list)
    confirm_token: str | None = None


class FieldOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    field_key: str
    value: str | None
    confidence: float
    needs_review: bool
    source_clause_id: str | None
    evidence: str | None
    verified: bool | None
    verify_note: str | None
