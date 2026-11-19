"""统一错误体：所有异常出口收敛到这里，HTTP 语义化。

响应形状（全项目唯一）::

    {"error": {"code": "NOT_FOUND", "message": "任务不存在", "detail": {...}, "request_id": "..."}}

request_id 是计划书里「一次请求所有日志可串联」的落点：客户端拿到它就能反查日志。
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = structlog.get_logger("app.errors")

STATUS_CODE_MAP: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "VALIDATION_ERROR",
    429: "TOO_MANY_REQUESTS",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


class AppError(Exception):
    """业务异常基类。子类只声明 status_code / code / 默认 message。"""

    status_code: int = 500
    code: str = "INTERNAL_ERROR"
    message: str = "服务内部错误"

    def __init__(
        self,
        message: str | None = None,
        detail: Any = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message or type(self).message
        self.detail = detail
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        super().__init__(self.message)


class BadRequest(AppError):
    status_code = 400
    code = "BAD_REQUEST"
    message = "请求参数不合法"


class Unauthorized(AppError):
    status_code = 401
    code = "UNAUTHORIZED"
    message = "缺少或无效的凭证"


class Forbidden(AppError):
    status_code = 403
    code = "FORBIDDEN"
    message = "无权访问该资源"


class NotFound(AppError):
    status_code = 404
    code = "NOT_FOUND"
    message = "资源不存在"


class Conflict(AppError):
    status_code = 409
    code = "CONFLICT"
    message = "资源状态冲突"


class RateLimited(AppError):
    status_code = 429
    code = "TOO_MANY_REQUESTS"
    message = "请求过于频繁"


class InternalError(AppError):
    status_code = 500
    code = "INTERNAL_ERROR"
    message = "服务内部错误"


def error_body(
    *,
    code: str,
    message: str,
    detail: Any = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "message": message, "detail": detail}
    if request_id:
        payload["request_id"] = request_id
    return {"error": payload}


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


# 默认 HTTP 短语翻成中文；业务自己抛的 detail 仍原样透出
_DEFAULT_MESSAGES: dict[int, str] = {
    400: "请求参数不合法",
    401: "缺少或无效的凭证",
    403: "无权访问该资源",
    404: "资源不存在",
    405: "请求方法不被允许",
    409: "资源状态冲突",
    413: "请求体过大",
    415: "不支持的媒体类型",
    422: "请求参数校验失败",
    429: "请求过于频繁",
    500: "服务内部错误",
    503: "服务暂不可用",
}


def _http_message(exc: StarletteHTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, str):
        try:
            is_default = detail == HTTPStatus(exc.status_code).phrase
        except ValueError:
            is_default = False
        if not is_default and detail.strip():
            return detail
    return _DEFAULT_MESSAGES.get(exc.status_code, "请求失败")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:  # noqa: ANN202
        rid = _request_id(request)
        log_fn = log.warning if exc.status_code < 500 else log.error
        log_fn(
            "app_error",
            status=exc.status_code,
            code=exc.code,
            message=exc.message,
            path=request.url.path,
        )
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(
                code=exc.code, message=exc.message, detail=exc.detail, request_id=rid
            ),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:  # noqa: ANN202
        # 校验失败统一 400 而不是 FastAPI 默认 422：契约里只有一个「参数不合法」出口
        detail = [
            {
                "loc": list(err.get("loc", [])),
                "msg": err.get("msg", ""),
                "type": err.get("type", ""),
            }
            for err in exc.errors()
        ]
        log.warning("validation_error", path=request.url.path, errors=len(detail))
        return JSONResponse(
            status_code=400,
            content=error_body(
                code="VALIDATION_ERROR",
                message="请求参数校验失败",
                detail=detail,
                request_id=_request_id(request),
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:  # noqa: ANN202
        code = STATUS_CODE_MAP.get(exc.status_code, "HTTP_ERROR")
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(
                code=code,
                message=_http_message(exc),
                detail=None,
                request_id=_request_id(request),
            ),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:  # noqa: ANN202
        # 兜底：对外不暴露堆栈，对内必须留完整异常
        log.exception("unhandled_error", path=request.url.path, error_type=type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content=error_body(
                code="INTERNAL_ERROR",
                message="服务内部错误",
                detail=None,
                request_id=_request_id(request),
            ),
        )
