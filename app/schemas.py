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
