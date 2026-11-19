"""结构化日志：request_id 贯穿一次请求的所有日志。"""

from __future__ import annotations

import logging
import sys

import structlog

from app.config import Settings


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # structlog 走自己的 PrintLogger，但 uvicorn / 第三方库走标准 logging，
    # 两条通道共用同一格式才不会出现「一半 JSON 一半彩色」。
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)

    shared: list = [
        structlog.contextvars.merge_contextvars,  # 把 bind_contextvars 的 request_id 带进每条日志
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.processors.StackInfoRenderer(),
    ]
    if settings.log_json:
        # JSON 渲染器不会自己格式化异常，必须显式加；ConsoleRenderer 自己会做，
        # 加了反而丢可读性（structlog 会warning）
        shared.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer(ensure_ascii=False)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "app") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
