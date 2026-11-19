"""指标聚合：从数据库算，而不是进程内计数器。

为什么不在内存里计数：进程重启后数字归零，多 worker 各算各的，
排查时对不上账。任务表、字段表、事件表本身就是事实来源，
聚合只是换个视角重新读一遍——代价是几条 count 查询，收益是数字可信。

两套输出：
- JSON（`/api/v1/metrics`）：给人看，带分位耗时
- Prometheus 文本（`/metrics`）：给监控抓，指标名稳定
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Any

from sqlalchemy import case, func
from sqlmodel import Session, select

from app.events import EventType
from app.models import Event, Task, TaskField, TaskStatus

TERMINAL = (TaskStatus.DONE.value, TaskStatus.BLOCKED.value, TaskStatus.FAILED.value)


@dataclass(slots=True)
class Metrics:
    tasks_by_status: dict[str, int] = field(default_factory=dict)
    tasks_total: int = 0
    success_total: int = 0
    failed_total: int = 0
    blocked_total: int = 0
    avg_duration_seconds: float | None = None
    p95_duration_seconds: float | None = None
    fields_total: int = 0
    fields_needs_review: int = 0
    fields_verified: int = 0
    fields_rejected: int = 0
    events_by_type: dict[str, int] = field(default_factory=dict)
    retry_total: int = 0

    @property
    def success_rate(self) -> float | None:
        finished = self.success_total + self.failed_total + self.blocked_total
        return None if finished == 0 else round(self.success_total / finished, 4)

    @property
    def review_rate(self) -> float | None:
        if self.fields_total == 0:
            return None
        return round(self.fields_needs_review / self.fields_total, 4)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tasks": {
                "total": self.tasks_total,
                "by_status": self.tasks_by_status,
                "success": self.success_total,
                "failed": self.failed_total,
                "blocked": self.blocked_total,
                "success_rate": self.success_rate,
                "avg_duration_seconds": self.avg_duration_seconds,
                "p95_duration_seconds": self.p95_duration_seconds,
            },
            "fields": {
                "total": self.fields_total,
                "needs_review": self.fields_needs_review,
                "verified": self.fields_verified,
                "rejected": self.fields_rejected,
                "review_rate": self.review_rate,
            },
            "events": {"by_type": self.events_by_type, "retries": self.retry_total},
        }


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return round(ordered[index], 3)


def collect(session: Session) -> Metrics:
    metrics = Metrics()

    rows = session.exec(select(Task.status, func.count()).group_by(Task.status)).all()
    metrics.tasks_by_status = {status: count for status, count in rows}
    metrics.tasks_total = sum(metrics.tasks_by_status.values())
    metrics.success_total = metrics.tasks_by_status.get(TaskStatus.DONE.value, 0)
    metrics.failed_total = metrics.tasks_by_status.get(TaskStatus.FAILED.value, 0)
    metrics.blocked_total = metrics.tasks_by_status.get(TaskStatus.BLOCKED.value, 0)

    durations: list[float] = []
    terminal_tasks = session.exec(select(Task).where(Task.status.in_(TERMINAL))).all()
    for task in terminal_tasks:
        if task.updated_at and task.created_at:
            durations.append((task.updated_at - task.created_at).total_seconds())
    if durations:
        metrics.avg_duration_seconds = round(mean(durations), 3)
        metrics.p95_duration_seconds = _percentile(durations, 0.95)

    field_rows = session.exec(
        select(
            func.count(),
            func.sum(case((TaskField.needs_review.is_(True), 1), else_=0)),
            func.sum(case((TaskField.verified.is_(True), 1), else_=0)),
            func.sum(case((TaskField.verified.is_(False), 1), else_=0)),
        )
    ).one()
    metrics.fields_total = int(field_rows[0] or 0)
    metrics.fields_needs_review = int(field_rows[1] or 0)
    metrics.fields_verified = int(field_rows[2] or 0)
    metrics.fields_rejected = int(field_rows[3] or 0)

    event_rows = session.exec(select(Event.type, func.count()).group_by(Event.type)).all()
    metrics.events_by_type = {event_type: count for event_type, count in event_rows}
    metrics.retry_total = metrics.events_by_type.get(EventType.RETRY.value, 0)

    return metrics


def prometheus_text(metrics: Metrics) -> str:
    """Prometheus 文本格式。指标名一旦对外就不要再改，改等于改契约。"""
    lines: list[str] = []

    def gauge(name: str, value: float | int | None, help_text: str) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {0 if value is None else value}")

    gauge("contract_tasks_total", metrics.tasks_total, "任务总数")
    gauge("contract_tasks_success", metrics.success_total, "已完成任务数")
    gauge("contract_tasks_failed", metrics.failed_total, "失败任务数")
    gauge("contract_tasks_blocked", metrics.blocked_total, "阻塞任务数")
    gauge("contract_tasks_success_rate", metrics.success_rate, "任务成功率")
    gauge("contract_task_duration_avg_seconds", metrics.avg_duration_seconds, "任务平均耗时")
    gauge("contract_task_duration_p95_seconds", metrics.p95_duration_seconds, "任务耗时 P95")
    gauge("contract_fields_total", metrics.fields_total, "字段记录总数")
    gauge("contract_fields_needs_review", metrics.fields_needs_review, "待人工确认字段数")
    gauge("contract_fields_verified", metrics.fields_verified, "证据通过字段数")
    gauge("contract_fields_rejected", metrics.fields_rejected, "证据不通过字段数")
    gauge("contract_extraction_retries_total", metrics.retry_total, "抽取重试次数")

    for status, count in sorted(metrics.tasks_by_status.items()):
        lines.append(f'contract_tasks_status{{status="{status}"}} {count}')

    return "\n".join(lines) + "\n"
