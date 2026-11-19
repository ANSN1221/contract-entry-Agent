"""证据校验工具：抽取值必须在原文里找得到。

两层判定，先便宜后贵：

1. **规则层**：把值和原文都归一化（去空格、全角转半角、去千分位）后做包含判断。
   命中就直接 supported，不花一次 LLM 调用。
2. **模型层**：规则没命中时（例如原文写「百分之五」而抽取值是 `5`），
   再让模型判一次。判不了就返回 `unverified`，绝不默认「通过」。

为什么不直接全交给模型：规则层能过的情况占多数，省 token；
更重要的是规则层的结论可复现、可审计，出了问题能指着代码说话。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.rag import RagUnavailable
from app.adapters.rag import get_clause as default_get_clause
from app.config import get_settings
from app.llm import LlmError, parse_json_object
from app.log import get_logger
from app.models import CONTRACT_FIELD_KEYS
from app.prompts import VERIFY_SYSTEM
from app.tools import ToolContext, ToolError, ToolSpec

log = get_logger("app.tools.verify_extraction")

Judge = Callable[[str, str, str], dict[str, Any]]


class VerifyExtractionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_key: str = Field(min_length=1, max_length=40, description="字段键，见字段清单")
    value: str = Field(min_length=1, max_length=500, description="待校验的抽取值")
    clause_id: str = Field(min_length=1, max_length=64, description="声称的出处条款 ID")


_DIGIT_TRANSLATION = str.maketrans("０１２３４５６７８９．，", "0123456789.,")


def normalize(text: str) -> str:
    """归一化：全角转半角、去空白、去千分位逗号，便于做包含比较。"""
    folded = unicodedata.normalize("NFKC", text).translate(_DIGIT_TRANSLATION)
    folded = folded.replace(",", "").replace("，", "")
    return re.sub(r"\s+", "", folded)


def rule_check(value: str, clause_text: str) -> bool:
    needle = normalize(value)
    if not needle:
        return False
    return needle in normalize(clause_text)


def llm_judge(messages_builder: Callable[[str, str, str], list[dict[str, str]]]) -> Judge:
    """构造一个走 DeepSeek 的判官。抽成工厂是为了测试能直接换掉。"""

    def _judge(field_key: str, value: str, clause_text: str) -> dict[str, Any]:
        from app.llm import LlmClient

        with LlmClient() as client:
            result = client.chat(
                messages_builder(field_key, value, clause_text),
                json_mode=True,
                temperature=0.0,
                max_tokens=300,
            )
        return parse_json_object(result.content)

    return _judge


def _build_judge_messages(field_key: str, value: str, clause_text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": VERIFY_SYSTEM},
        {
            "role": "user",
            "content": (
                f"字段：{field_key}\n抽取值：{value}\n\n出处条款原文：\n{clause_text}\n\n"
                "请判断该值是否被原文支持，只输出 JSON。"
            ),
        },
    ]


def handle(args: VerifyExtractionArgs, context: ToolContext) -> dict[str, Any]:
    if args.field_key not in CONTRACT_FIELD_KEYS:
        raise ToolError(
            "UNKNOWN_FIELD",
            f"未知字段：{args.field_key}",
            detail={"allowed": sorted(CONTRACT_FIELD_KEYS)},
        )

    get_clause = context.extras.get("get_clause") or default_get_clause
    try:
        clause = get_clause(args.clause_id)
    except RagUnavailable as exc:
        raise ToolError("RAG_UNAVAILABLE", str(exc)) from exc

    if clause is None:
        raise ToolError(
            "CLAUSE_NOT_FOUND",
            f"条款不存在：{args.clause_id}",
            detail={"clause_id": args.clause_id},
        )

    if rule_check(args.value, clause.text):
        return _result(args, "supported", "rule", "原文逐字包含该值", clause)

    settings = get_settings()
    use_llm = context.extras.get("verify_with_llm", settings.verify_with_llm)
    if not use_llm:
        return _result(args, "unverified", "none", "规则层未命中，且未启用模型复核", clause)

    judge: Judge = context.extras.get("judge") or llm_judge(_build_judge_messages)
    try:
        verdict = judge(args.field_key, args.value, clause.text)
    except LlmError as exc:
        # 复核失败不等于伪造：结论是「未验证」，交由人工确认
        log.warning("verify_llm_failed", clause_id=args.clause_id, error=str(exc))
        return _result(args, "unverified", "none", f"模型复核失败：{exc}", clause)

    supported = bool(verdict.get("supported"))
    reason = str(verdict.get("reason") or "").strip() or ("原文支持" if supported else "原文不支持")
    return _result(args, "supported" if supported else "unsupported", "llm", reason, clause)


def _result(
    args: VerifyExtractionArgs,
    status: str,
    method: str,
    reason: str,
    clause: Any,
) -> dict[str, Any]:
    return {
        "field_key": args.field_key,
        "value": args.value,
        "clause_id": args.clause_id,
        "citation": clause.citation_label,
        "page": f"{clause.page_start}-{clause.page_end}",
        "status": status,
        "method": method,
        "reason": reason,
        "supported": status == "supported",
    }


SPEC = ToolSpec(
    name="verify_extraction",
    description=(
        "校验某个字段的抽取值是否被指定条款原文支持。返回 supported / unsupported / "
        "unverified 三态：unverified 表示无法判断，需要人工确认，不能当作通过。"
    ),
    args_model=VerifyExtractionArgs,
    handler=handle,
)
