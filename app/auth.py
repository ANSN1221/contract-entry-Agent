"""鉴权：自签短期 token + 静态 token 兜底。

为什么不用 JWT 库：这个项目只需要「签发 → 校验 → 过期」三件事，
HS256 三行代码就能覆盖，引一个库反而要多解释一轮算法白名单、时钟偏移、
密钥轮换那些用不上的配置。

token 形状（点分三段，便于日志里肉眼辨认）::

    v1.<base64url(payload)>.<base64url(hmac_sha256)>

payload = {"sub": 主体, "iat": 签发时间, "exp": 过期时间}（unix 秒）。

三档策略，按配置自动升降：

| 配置 | 行为 |
| --- | --- |
| 配了 `API_TOKEN_SECRET` | 只认签名 token（+ 静态 token，若也配了） |
| 只配了 `API_TOKEN` | 只认这个长期 token，适合开发/内网 |
| 都没配 + dev/test | 放行，并在启动日志里 warning |
| 都没配 + prod | 全部拒绝（fail closed），绝不裸奔 |
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings
from app.errors import Unauthorized
from app.log import get_logger

log = get_logger("app.auth")

#: 只在 OpenAPI 里声明 Bearer 方案，让 /docs 出现 Authorize 按钮；
#: 真正的解析仍走 authenticate()，以便兼容 X-API-Key。
bearer_scheme = HTTPBearer(auto_error=False, description="Bearer <token>")

TOKEN_PREFIX = "v1"
SECRET_MIN_LENGTH = 16


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(body: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return _b64encode(digest)


@dataclass(frozen=True, slots=True)
class Principal:
    """认证通过后的调用方身份。目前只有主体名，扩展权限时往这里加字段。"""

    subject: str
    expires_at: int | None
    via: str  # "signed" | "static"

    @property
    def is_static(self) -> bool:
        return self.via == "static"


def issue_token(
    subject: str,
    *,
    secret: str,
    ttl_seconds: int = 3600,
    now: int | None = None,
) -> str:
    """签发 token。`ttl_seconds` 给负数可用来造过期样本（测试用）。"""
    if not subject.strip():
        raise ValueError("subject 不能为空")
    if len(secret) < SECRET_MIN_LENGTH:
        raise ValueError(f"secret 至少 {SECRET_MIN_LENGTH} 个字符")

    issued_at = int(time.time()) if now is None else int(now)
    payload = {"sub": subject, "iat": issued_at, "exp": issued_at + int(ttl_seconds)}
    body = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{TOKEN_PREFIX}.{body}.{_sign(body, secret)}"


def verify_token(token: str, *, secret: str, now: int | None = None) -> Principal:
    """校验签名与有效期。任何一步不对都只回 401，不告诉对方错在哪一步。"""
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        raise Unauthorized("凭证格式不正确")

    _, body, signature = parts
    if not hmac.compare_digest(signature, _sign(body, secret)):
        raise Unauthorized("凭证签名校验失败")

    try:
        payload = json.loads(_b64decode(body))
        subject = str(payload["sub"])
        expires_at = int(payload["exp"])
    except (ValueError, KeyError, TypeError) as exc:
        raise Unauthorized("凭证内容无法解析") from exc

    current = int(time.time()) if now is None else int(now)
    if expires_at <= current:
        raise Unauthorized("凭证已过期", detail={"expired_at": expires_at})

    return Principal(subject=subject, expires_at=expires_at, via="signed")


def _extract_token(request: Request) -> str | None:
    """同时接受 Authorization: Bearer xxx 和 X-API-Key: xxx。"""
    header = request.headers.get("Authorization", "")
    if header:
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
        return None
    api_key = request.headers.get("X-API-Key", "").strip()
    return api_key or None


def authenticate(request: Request, settings: Settings) -> Principal | None:
    """返回身份；未配置任何凭证且非生产环境时返回 None 表示放行。"""
    token = _extract_token(request)
    signed_enabled = bool(settings.api_token_secret)

    if token:
        if signed_enabled:
            try:
                return verify_token(token, secret=settings.api_token_secret)
            except Unauthorized:
                # 签名不通过时再给静态 token 一次机会（两套凭证共存期）
                if settings.api_token and hmac.compare_digest(token, settings.api_token):
                    return Principal(subject="static", expires_at=None, via="static")
                raise
        if settings.api_token and hmac.compare_digest(token, settings.api_token):
            return Principal(subject="static", expires_at=None, via="static")
        raise Unauthorized("凭证无效")

    # 没带凭证
    if not settings.auth_enabled:
        if settings.is_prod:
            # fail closed：生产环境没配置凭证是部署事故，不能放行
            raise Unauthorized("服务未配置访问凭证")
        return None
    raise Unauthorized("缺少访问凭证")


async def require_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> Principal | None:
    """FastAPI 依赖。挂在 v1 路由上，一次挂载覆盖后续所有接口。"""
    settings: Settings = request.app.state.settings
    principal = authenticate(request, settings)
    if principal is None:
        # 开发环境放行，但留痕：方便日后回看“这段为什么没鉴权”
        log.debug("auth_disabled", path=request.url.path)
    else:
        request.state.principal = principal
    return principal
