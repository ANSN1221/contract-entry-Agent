"""工具层：白名单 + 参数强校验 + 统一出口。

Agent 能调用的东西必须可枚举、可校验、可审计。三条规矩：

1. **白名单**：`TOOL_WHITELIST` 里没有的名字，一律拒绝。模型幻觉出一个
   `delete_all_tasks` 时，我们回的是「未知工具」，而不是试着去执行。
2. **参数强校验**：每个工具一个 pydantic 模型，`extra="forbid"`。
   模型多塞一个字段就报错——宁可报错也不猜它的意思。
3. **统一出口**：工具不抛裸异常给编排层，失败翻译成 `ToolError`（带 code），
   编排层据此决定重试、降级还是转 blocked。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from app.adapters.rag import ClauseHit
from app.log import get_logger

log = get_logger("app.tools")


class ToolError(Exception):
    """工具执行失败。code 供编排层判断是否可重试。"""

    def __init__(self, code: str, message: str, detail: Any = None) -> None:
        self.code = code
        self.message = message
        self.detail = detail
        super().__init__(message)


@dataclass(slots=True)
class ToolContext:
    """工具执行上下文。检索器可注入，单测不必依赖真实索引。"""

    retriever: Callable[..., list[ClauseHit]] | None = None
    task_id: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[..., dict[str, Any]]


def _registry() -> dict[str, ToolSpec]:
    # 延迟导入：避免 app.tools 与具体工具模块循环依赖
    from app.tools.search_clauses import SPEC as search_spec
    from app.tools.verify_extraction import SPEC as verify_spec

    return {spec.name: spec for spec in (search_spec, verify_spec)}


def list_tools() -> list[dict[str, Any]]:
    """导出给 LLM 的工具清单（OpenAI function calling 形状）。"""
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.args_model.model_json_schema(),
        }
        for spec in _registry().values()
    ]


def tool_names() -> list[str]:
    return list(_registry())


def run_tool(
    name: str,
    raw_args: dict[str, Any] | None = None,
    *,
    context: ToolContext | None = None,
) -> dict[str, Any]:
    """白名单 → 参数校验 → 执行。所有失败都以 ToolError 抛出。"""
    registry = _registry()
    spec = registry.get(name)
    if spec is None:
        raise ToolError(
            "UNKNOWN_TOOL",
            f"未知工具：{name}",
            detail={"allowed": sorted(registry)},
        )

    try:
        args = spec.args_model.model_validate(raw_args or {})
    except ValidationError as exc:
        # 只回错误摘要，不把整份 schema 塞进上下文
        problems = [
            {"loc": ".".join(str(p) for p in err["loc"]), "msg": err["msg"]} for err in exc.errors()
        ]
        raise ToolError(
            "INVALID_ARGS",
            f"工具 {name} 参数不合法",
            detail={"problems": problems},
        ) from exc

    log.info("tool_started", tool=name, task_id=(context.task_id if context else None))
    try:
        return spec.handler(args, context or ToolContext())
    except ToolError:
        raise
    except Exception as exc:  # 工具内部未预料的异常，也不能让编排层直接崩
        log.exception("tool_failed", tool=name)
        raise ToolError(
            "TOOL_EXECUTION_FAILED",
            f"工具 {name} 执行失败：{type(exc).__name__}",
            detail={"error": str(exc)[:200]},
        ) from exc
