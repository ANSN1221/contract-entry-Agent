"""字段抽取：召回条款 → 一次 LLM 调用 → 结构化草稿。

设计取舍：

- **一次调用抽全部 12 个字段**，不是每字段一次。12 次调用既慢又贵，
  而且字段之间有上下文（比如总价与付款比例要一起看）。
- **输出必须带出处**（clause_id + quote），没有出处的值一律降级为待确认，
  这是「引用可核对」的底线。
- **LLM 输出不可信的部分由代码兜底**：字段键不在清单内就丢弃、置信度越界就夹紧、
  clause_id 不在本次召回范围内就把证据清空并降置信度。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.adapters.rag import ClauseHit
from app.llm import LlmError, parse_json_object
from app.log import get_logger
from app.models import CONTRACT_FIELD_KEYS
from app.prompts import EXTRACTION_SYSTEM, build_extraction_user_prompt

log = get_logger("app.extract")

MAX_CLAUSES = 16


class ChatLike(Protocol):
    """只依赖「能聊一句」这个能力，测试可以塞任意替身。"""

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        temperature: float = 0.0,
        max_tokens: int = 1200,
    ) -> Any: ...


@dataclass(slots=True)
class FieldDraft:
    field_key: str
    value: str | None
    confidence: float
    clause_id: str | None = None
    quote: str | None = None
    needs_review: bool = False

    @property
    def is_empty(self) -> bool:
        return self.value is None or not self.value.strip()

    def as_dict(self) -> dict[str, Any]:
        return {
            "field_key": self.field_key,
            "value": self.value,
            "confidence": round(self.confidence, 3),
            "clause_id": self.clause_id,
            "quote": self.quote,
            "needs_review": self.needs_review,
        }


@dataclass(slots=True)
class ExtractionResult:
    drafts: list[FieldDraft]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def missing_keys(self) -> list[str]:
        return sorted(CONTRACT_FIELD_KEYS - {d.field_key for d in self.drafts})

    @property
    def review_keys(self) -> list[str]:
        return [d.field_key for d in self.drafts if d.needs_review]

    @property
    def filled(self) -> int:
        return sum(1 for d in self.drafts if not d.is_empty)

    def by_key(self) -> dict[str, FieldDraft]:
        return {d.field_key: d for d in self.drafts}


def collect_clauses(
    retriever: Any,
    queries: list[str],
    *,
    top_k: int = 6,
    doc_ids: list[str] | None = None,
) -> list[ClauseHit]:
    """多组关键词召回后按 clause_id 去重，控制总条数，避免把上下文塞爆。

    `doc_ids` 透传给检索器：多合同语料下必须限定在当前合同内，
    否则每组查询的前几条会被别的合同占满（实测目标合同只召回 1 条）。
    """
    seen: dict[str, ClauseHit] = {}
    extra = {"doc_ids": doc_ids} if doc_ids else {}
    for query in queries:
        for hit in retriever(query, top_k=top_k, **extra):
            seen.setdefault(hit.clause_id, hit)
    ordered = sorted(seen.values(), key=lambda h: (h.page_start, h.clause_id))
    return ordered[:MAX_CLAUSES]


def _clamp_confidence(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, value))


def _normalize_value(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return "是" if raw else "否"
    if isinstance(raw, int | float):
        return f"{raw}"
    text = str(raw).strip()
    return text or None


def parse_fields(
    payload: dict[str, Any],
    *,
    allowed_clause_ids: set[str],
    threshold: float,
) -> tuple[list[FieldDraft], list[str]]:
    """把模型输出翻译成草稿，并做四道兜底校验。"""
    warnings: list[str] = []
    drafts: list[FieldDraft] = []
    seen: set[str] = set()

    items = payload.get("fields")
    if not isinstance(items, list):
        return [], ["模型输出缺少 fields 数组"]

    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        if key not in CONTRACT_FIELD_KEYS:
            warnings.append(f"丢弃未知字段：{key or '(空)'}")
            continue
        if key in seen:
            warnings.append(f"字段重复，保留首次结果：{key}")
            continue
        seen.add(key)

        value = _normalize_value(item.get("value"))
        confidence = _clamp_confidence(item.get("confidence"))
        clause_id = item.get("clause_id")
        clause_id = str(clause_id).strip() if clause_id else None
        quote = item.get("quote")
        quote = str(quote).strip() if quote else None

        # 证据不在本次召回范围内：值可能对，但无法核对，降级待人确认
        if clause_id and clause_id not in allowed_clause_ids:
            warnings.append(f"{key} 的 clause_id 不在召回范围内：{clause_id}")
            clause_id = None
            quote = None
            confidence = min(confidence, 0.3)

        if value is not None and not clause_id:
            confidence = min(confidence, 0.3)
            warnings.append(f"{key} 有值但无出处，降为待确认")

        needs_review = value is None or confidence < threshold
        drafts.append(
            FieldDraft(
                field_key=key,
                value=value,
                confidence=confidence,
                clause_id=clause_id,
                quote=quote,
                needs_review=needs_review,
            )
        )

    for key in CONTRACT_FIELD_KEYS - seen:
        drafts.append(FieldDraft(field_key=key, value=None, confidence=0.0, needs_review=True))

    drafts.sort(key=lambda d: d.field_key)
    return drafts, warnings


def extract_fields(
    clauses: list[ClauseHit],
    *,
    client: ChatLike,
    threshold: float = 0.7,
) -> ExtractionResult:
    """对给定条款做一次抽取。条款为空时直接返回全空草稿，不浪费一次调用。"""
    if not clauses:
        return ExtractionResult(
            drafts=[
                FieldDraft(field_key=key, value=None, confidence=0.0, needs_review=True)
                for key in sorted(CONTRACT_FIELD_KEYS)
            ],
            error="没有召回条款，未执行抽取",
        )

    messages = [
        {"role": "system", "content": EXTRACTION_SYSTEM},
        {
            "role": "user",
            "content": build_extraction_user_prompt([(c.clause_id, c.text) for c in clauses]),
        },
    ]

    try:
        result = client.chat(messages, json_mode=True, temperature=0.0, max_tokens=1800)
    except LlmError as exc:
        log.warning("extract_llm_failed", error=str(exc), retryable=exc.retryable)
        return ExtractionResult(
            drafts=[
                FieldDraft(field_key=key, value=None, confidence=0.0, needs_review=True)
                for key in sorted(CONTRACT_FIELD_KEYS)
            ],
            error=str(exc),
        )

    try:
        payload = parse_json_object(result.content)
    except LlmError as exc:
        return ExtractionResult(drafts=[], error=str(exc), warnings=["模型输出无法解析"])

    drafts, warnings = parse_fields(
        payload,
        allowed_clause_ids={c.clause_id for c in clauses},
        threshold=threshold,
    )
    return ExtractionResult(
        drafts=drafts,
        prompt_tokens=getattr(result, "prompt_tokens", 0),
        completion_tokens=getattr(result, "completion_tokens", 0),
        warnings=warnings,
    )
