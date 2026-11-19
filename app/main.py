"""应用工厂：装配顺序 = 日志 → 错误体 → 中间件 → 路由。

用工厂而不是模块级单例，测试才能拿到互不干扰的实例。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.v1 import api_router, health
from app.auth import require_principal
from app.config import Settings, get_settings
from app.errors import register_exception_handlers
from app.log import configure_logging, get_logger
from app.middleware import RateLimitMiddleware, RequestContextMiddleware
from app.runner import build_runner
from app.store import get_database

log = get_logger("app.main")


def _check_schema() -> None:
    """启动自检：库没迁移时直接在日志里喊出来。

    不做自动建表——`create_all` 只能建新表、不会改老表，拿它当迁移工具
    迟早在线上翻车。这里只负责「让问题在启动时暴露，而不是等第一个请求 500」。
    """
    try:
        with get_database().session() as session:
            session.exec(text("SELECT 1 FROM task LIMIT 1"))
    except Exception as exc:
        log.error(
            "schema_not_ready",
            error=str(exc)[:120],
            hint="先执行 uv run alembic upgrade head",
        )


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
        _check_schema()
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

    # 前端构建产物存在时直接由后端托管：部署只需要一个进程、一个端口
    dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
        log.info("frontend_mounted", path=str(dist))

    return app


app = create_app()
