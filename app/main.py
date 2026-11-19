"""应用工厂：装配顺序 = 日志 → 错误体 → 中间件 → 路由。

用工厂而不是模块级单例，测试才能拿到互不干扰的实例。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app.api.v1 import api_router, health
from app.auth import require_principal
from app.config import Settings, get_settings
from app.errors import register_exception_handlers
from app.log import configure_logging, get_logger
from app.middleware import RateLimitMiddleware, RequestContextMiddleware
from app.runner import build_runner
from app.store import get_database

log = get_logger("app.main")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "app_started",
            service=settings.app_name,
            version=settings.version,
            env=settings.env,
        )
        # 调度器随应用起停：任务状态在库里，所以进程重启不会丢任务
        runner = build_runner(settings, get_database())
        app.state.runner = runner
        await runner.start()
        try:
            yield
        finally:
            await runner.stop()
        log.info("app_stopped")

    app = FastAPI(
        title="合同录入执行 Agent",
        version=settings.version,
        summary="上传合同 PDF → 抽取字段 → 人工确认 → 写库",
        lifespan=lifespan,
    )
    app.state.settings = settings

    register_exception_handlers(app)
    # 后加的在更外层：RequestContext 包住限流，被限流的请求也有 request_id 和访问日志
    app.add_middleware(RateLimitMiddleware, settings=settings)
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router)
    # 鉴权挂在整组 v1 路由上：后续新增接口自动受保护，不用逐个记得加
    app.include_router(api_router, prefix="/api/v1", dependencies=[Depends(require_principal)])
    return app


app = create_app()
