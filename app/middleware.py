"""HTTP 中间件：request_id 贯穿 + 访问日志 + 限流。

顺序（add_middleware 后进先出）：RequestContext 在外层，限流在内层，
这样被限流的请求同样带 request_id、同样有访问日志。
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.config import Settings
from app.errors import RateLimited, error_body

log = structlog.get_logger("app.access")

REQUEST_ID_HEADER = "X-Request-ID"
_SKIP_LOG_PATHS = frozenset({"/healthz", "/metrics"})

# 不信任外部传入的超长串，避免污染日志
_MAX_REQUEST_ID_LEN = 64


class RequestContextMiddleware(BaseHTTPMiddleware):
    """生成/透传 request_id，并记录一条结构化的访问日志。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = incoming if 0 < len(incoming) <= _MAX_REQUEST_ID_LEN else uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id, method=request.method, path=request.url.path
        )

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # 未捕获异常也要留一条带耗时的记录，否则日志里只有 handler 那一条
            log.exception(
                "request_failed", duration_ms=round((time.perf_counter() - started) * 1000, 2)
            )
            raise
        finally:
            structlog.contextvars.clear_contextvars()

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers[REQUEST_ID_HEADER] = request_id
        # 排查耗时用 Server-Timing，浏览器 DevTools 可直接看到
        response.headers["Server-Timing"] = f"app;dur={duration_ms}"

        if request.url.path not in _SKIP_LOG_PATHS:
            log.info(
                "request_finished",
                status=response.status_code,
                duration_ms=duration_ms,
            )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """单机滑动窗口限流。

    计划书明确不做分布式队列，多实例上线时这一层要换成 Redis 计数，
    但接口形状保持不变（超限返回统一错误体 + Retry-After）。
    """

    def __init__(self, app, settings: Settings) -> None:  # noqa: ANN001
        super().__init__(app)
        self.settings = settings
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _key(self, request: Request) -> str:
        # 有凭证按凭证算，否则按来源 IP；避免同一 NAT 下互相拖累
        token = request.headers.get("Authorization", "")
        if token:
            return f"token:{token[-12:]}"
        return f"ip:{request.client.host if request.client else 'unknown'}"

    def _over_limit(self, key: str, now: float, limit: int) -> bool:
        window = self._hits[key]
        while window and now - window[0] >= 60.0:
            window.popleft()
        if len(window) >= limit:
            return True
        window.append(now)
        return False

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self.settings.rate_limit_enabled:
            return await call_next(request)

        limit = self.settings.rate_limit_per_minute
        key = self._key(request)
        now = time.monotonic()
        if self._over_limit(key, now, limit):
            retry_after = max(1, int(60 - (now - self._hits[key][0])))
            log.warning("rate_limited", key=key, limit=limit)
            exc = RateLimited(detail={"limit_per_minute": limit, "retry_after": retry_after})
            return JSONResponse(
                status_code=exc.status_code,
                content=error_body(
                    code=exc.code,
                    message=exc.message,
                    detail=exc.detail,
                    request_id=getattr(request.state, "request_id", None),
                ),
                headers={
                    "Retry-After": str(retry_after),
                    REQUEST_ID_HEADER: getattr(request.state, "request_id", ""),
                },
            )
        return await call_next(request)
