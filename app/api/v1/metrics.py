"""指标接口（JSON 版）。给人和前端看，带分位耗时与分布。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.observability import collect
from app.store import get_database

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("", summary="指标快照（JSON）")
async def metrics_snapshot() -> dict[str, Any]:
    with get_database().session() as session:
        return collect(session).as_dict()
