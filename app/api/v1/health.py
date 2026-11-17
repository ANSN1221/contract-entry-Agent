"""系统探针路由：部署平台与 README 验收都靠这两个端点。"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["system"])


@router.get("/healthz", summary="存活探针")
async def healthz(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.version,
        "env": settings.env,
    }


@router.get("/readyz", summary="就绪探针")
async def readyz(request: Request) -> dict[str, object]:
    """就绪 = 依赖可用。D2 接入数据库后这里补 DB 连通性检查。"""
    settings = request.app.state.settings
    return {"status": "ready", "checks": {"db": "not_configured", "env": settings.env}}
