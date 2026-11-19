"""系统探针路由：部署平台与 README 验收都靠这两个端点。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from app.observability import collect, prometheus_text
from app.store import get_database

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


@router.get(
    "/metrics",
    summary="Prometheus 指标",
    response_class=PlainTextResponse,
    responses={200: {"content": {"text/plain": {}}}},
)
async def metrics() -> str:
    """给监控抓。不做鉴权：与探针同理，抓不到指标的监控等于没有监控。"""
    with get_database().session() as session:
        return prometheus_text(collect(session))
