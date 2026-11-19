"""LLM 客户端：最小 HTTP 实现 + 重试 + token 记账。

不引官方 SDK 的理由与鉴权那边一致：只需要 `chat/completions` 一个端点，
自己写能精确控制重试边界、超时和记账字段，也少一个依赖。

重试策略（都是可解释的）：

| 情况 | 处理 |
| --- | --- |
| 连接失败 / 读超时 | 重试，指数退避 |
| 429 限流 | 重试，优先读 `Retry-After` |
| 5xx | 重试 |
| 400 / 401 / 403 | 立刻失败，重试没有意义 |

`trust_env=False` 是刻意的：本机系统代理会把外网请求转成 502，
而模型接口是直连可用的。要代理时改这一个参数即可。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.log import get_logger

log = get_logger("app.llm")

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class LlmError(Exception):
    """LLM 调用失败。`retryable` 供编排层决定要不要再来一次。"""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        detail: Any = None,
    ) -> None:
        self.status = status
        self.retryable = retryable
        self.detail = detail
        super().__init__(message)


@dataclass(slots=True)
class ChatResult:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    attempts: int = 1
    duration_ms: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def parse_json_object(content: str) -> dict[str, Any]:
    """从模型输出里取出 JSON 对象。

    模型偶尔会用 ```json 包一层，或在前后说两句废话，这里只做最小兜底：
    先直接解析，失败就剥掉代码围栏再试，最后退化为「取第一个 { 到最后一个 }」。
    """
    text = content.strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    if text.startswith("```"):
        body = text.split("```")[1] if text.count("```") >= 2 else text
        body = body.removeprefix("json").strip()
        try:
            value = json.loads(body)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            value = json.loads(text[start : end + 1])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass

    raise LlmError("模型输出不是合法 JSON", retryable=True, detail={"preview": text[:200]})


class LlmClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = httpx.Client(
            base_url=self.settings.deepseek_base_url,
            timeout=httpx.Timeout(self.settings.llm_timeout_seconds),
            trust_env=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LlmClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.deepseek_api_key}",
            "Content-Type": "application/json",
        }

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        temperature: float = 0.0,
        max_tokens: int = 1200,
    ) -> ChatResult:
        if not self.settings.deepseek_api_key:
            raise LlmError("未配置 DEEPSEEK_API_KEY", retryable=False)

        payload: dict[str, Any] = {
            "model": self.settings.deepseek_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_error: LlmError | None = None
        for attempt in range(1, self.settings.llm_max_retries + 1):
            started = time.perf_counter()
            try:
                response = self._client.post(
                    "/chat/completions", headers=self._headers, json=payload
                )
            except httpx.HTTPError as exc:
                last_error = LlmError(
                    f"调用模型失败：{type(exc).__name__}", retryable=True, detail=str(exc)[:200]
                )
                self._backoff(attempt, last_error, error_code=None)
                continue

            duration_ms = round((time.perf_counter() - started) * 1000, 2)

            if response.status_code in RETRYABLE_STATUS:
                last_error = LlmError(
                    f"模型返回 {response.status_code}",
                    status=response.status_code,
                    retryable=True,
                    detail=response.text[:200],
                )
                self._backoff(attempt, last_error, response.headers.get("Retry-After"))
                continue

            if response.status_code >= 400:
                raise LlmError(
                    f"模型调用被拒绝：{response.status_code}",
                    status=response.status_code,
                    retryable=False,
                    detail=response.text[:200],
                )

            data = response.json()
            try:
                content = data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError) as exc:
                raise LlmError("模型返回结构异常", detail=str(data)[:200]) from exc

            usage = data.get("usage", {}) or {}
            result = ChatResult(
                content=content,
                model=data.get("model", self.settings.deepseek_model),
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                attempts=attempt,
                duration_ms=duration_ms,
                raw=data,
            )
            log.info(
                "llm_call",
                model=result.model,
                attempt=attempt,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                duration_ms=duration_ms,
            )
            return result

        raise last_error or LlmError("调用模型失败")

    def _backoff(self, attempt: int, error: LlmError, error_code: str | None) -> None:
        if attempt >= self.settings.llm_max_retries:
            log.warning("llm_retry_exhausted", attempt=attempt, error=str(error))
            raise error
        delay = self.settings.llm_retry_base_delay * (2 ** (attempt - 1))
        if error_code:
            try:
                delay = max(delay, float(error_code))
            except ValueError:
                pass
        delay = min(delay, 8.0)
        log.warning("llm_retry", attempt=attempt, delay=round(delay, 2), error=str(error))
        time.sleep(delay)
