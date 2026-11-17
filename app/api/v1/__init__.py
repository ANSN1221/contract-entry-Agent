"""v1 业务路由汇总。D3 起陆续挂上 tasks / metrics 等子路由。"""

from __future__ import annotations

from fastapi import APIRouter

api_router = APIRouter()

__all__ = ["api_router"]
