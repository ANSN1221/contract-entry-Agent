"""v1 业务路由汇总。D3 起陆续挂上 tasks / metrics 等子路由。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import events, tasks

api_router = APIRouter()
api_router.include_router(tasks.router)
api_router.include_router(events.router)

__all__ = ["api_router"]
