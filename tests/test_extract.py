"""抽取验收：输出兜底、降级规则、指标与失败路径。

全部用假模型，不打网络。真实模型验证放在 `test_extract_live.py`（需要密钥）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.adapters.rag import ClauseHit
from app.extract import collect_clauses, extract_fields, parse_fields
from app.llm import LlmError
from app.models import CONTRACT_FIELD_KEYS

CLAUSES = [
    ClauseHit(
        clause_id="DOC901-C0003",
        text="本合同总价为人民币壹佰贰拾捌万元整（¥1,280,000.00 元）。",
        citation_label="第 1 页 第二条",
        page_start=1,
        page_end=1,
        score=0.9,
    ),
    ClauseHit(
        clause_id="DOC901-C0008",
        text="乙方应向甲方提交合同总价百分之五的履约保证金。",
        citation_label="第 1 页 第七条",
        page_start=1,
        page_end=1,
        score=0.8,
    ),
]


@dataclass
class FakeChat:
    content: str
    prompt_tokens: int = 120
    completion_tokens: int = 80

    def chat(self, messages, *, json_mode=False, temperature=0.0, max_tokens=1200):  # noqa: ANN001
        self.last_messages = messages
        self.json_mode = json_mode
        return self


def payload(**overrides) -> dict:
    fields = overrides.pop(
        "fields",
        [
            {
                "key": "total_amount",
                "value": "1280000.00",
                "confidence": 0.95,
                "clause_id": "DOC901-C0003",
                "quote": "¥1,280,000.00 元",
            },
            {
                "key": "deposit_ratio",
                "value": "5",
                "confidence": 0.6,
                "clause_id": "DOC901-C0008",
                "quote": "百分之五",
            },
        ],
    )
    return {"fields": fields, **overrides}


# ---------- 解析兜底 ----------


def test_parse_keeps_known_fields_and_fills_missing() -> None:
    drafts, warnings = parse_fields(
        payload(), allowed_clause_ids={"DOC901-C0003", "DOC901-C0008"}, threshold=0.7
    )
    by_key = {d.field_key: d for d in drafts}

    assert len(drafts) == 12  # 12 个字段全都有一行，缺的给空
    assert by_key["total_amount"].value == "1280000.00"
    assert by_key["total_amount"].needs_review is False
    # 0.6 低于阈值 0.7 → 标红待确认
    assert by_key["deposit_ratio"].needs_review is True
    assert by_key["warranty_period"].value is None
    assert by_key["warranty_period"].needs_review is True
    assert warnings == []


def test_unknown_field_dropped_with_warning() -> None:
    drafts, warnings = parse_fields(
        payload(
            fields=[
                {
                    "key": "party_a",
                    "value": "甲公司",
                    "confidence": 0.9,
                    "clause_id": "DOC901-C0003",
                },
                {
                    "key": "handwritten_note",
                    "value": "x",
                    "confidence": 0.9,
                    "clause_id": "DOC901-C0003",
                },
            ]
        ),
        allowed_clause_ids={"DOC901-C0003"},
        threshold=0.7,
    )
    assert {d.field_key for d in drafts} == CONTRACT_FIELD_KEYS
    assert any("handwritten_note" in w for w in warnings)


def test_clause_outside_recall_clears_evidence() -> None:
    """出处不在本次召回范围内 → 证据清空、置信度压到 0.3、待人确认。"""
    drafts, warnings = parse_fields(
        payload(
            fields=[
                {
                    "key": "party_a",
                    "value": "江南智造科技有限公司",
                    "confidence": 0.95,
                    "clause_id": "FAKE-C0001",
                    "quote": "x",
                }
            ]
        ),
        allowed_clause_ids={"DOC901-C0003"},
        threshold=0.7,
    )
    draft = {d.field_key: d for d in drafts}["party_a"]
    assert draft.clause_id is None
    assert draft.quote is None
    assert draft.confidence <= 0.3
    assert draft.needs_review is True
    assert any("不在召回范围" in w for w in warnings)


def test_confidence_is_clamped() -> None:
    drafts, _ = parse_fields(
        payload(
            fields=[
                {"key": "party_a", "value": "甲", "confidence": 1.8, "clause_id": "DOC901-C0003"},
                {"key": "party_b", "value": "乙", "confidence": -0.5, "clause_id": "DOC901-C0003"},
            ]
        ),
        allowed_clause_ids={"DOC901-C0003"},
        threshold=0.7,
    )
    values = {d.field_key: d.confidence for d in drafts}
    assert values["party_a"] == 1.0
    assert values["party_b"] == 0.0


def test_duplicate_field_keeps_first() -> None:
    drafts, warnings = parse_fields(
        payload(
            fields=[
                {"key": "party_a", "value": "先", "confidence": 0.9, "clause_id": "DOC901-C0003"},
                {"key": "party_a", "value": "后", "confidence": 0.9, "clause_id": "DOC901-C0003"},
            ]
        ),
        allowed_clause_ids={"DOC901-C0003"},
        threshold=0.7,
    )
    assert {d.field_key: d for d in drafts}["party_a"].value == "先"
    assert any("重复" in w for w in warnings)


def test_missing_fields_array() -> None:
    drafts, warnings = parse_fields({"oops": 1}, allowed_clause_ids=set(), threshold=0.7)
    assert drafts == []
    assert warnings == ["模型输出缺少 fields 数组"]


# ---------- 完整抽取 ----------


def test_extract_returns_drafts_and_tokens() -> None:
    client = FakeChat(content=json.dumps(payload(), ensure_ascii=False))
    result = extract_fields(CLAUSES, client=client, threshold=0.7)

    assert result.filled == 2
    # 低置信的那条 + 10 个没抽到的字段，都要进确认卡
    assert len(result.review_keys) == 11
    assert "deposit_ratio" in result.review_keys
    assert "total_amount" not in result.review_keys
    assert result.prompt_tokens == 120
    assert result.completion_tokens == 80
    assert result.error == ""
    assert client.json_mode is True
    # 提示词里必须带上条款 ID，模型才有东西可引用
    assert "DOC901-C0003" in client.last_messages[-1]["content"]


def test_extract_accepts_fenced_json() -> None:
    fenced = "```json\n" + json.dumps(payload(), ensure_ascii=False) + "\n```"
    result = extract_fields(CLAUSES, client=FakeChat(content=fenced))
    assert result.filled == 2


def test_extract_without_clauses_skips_llm() -> None:
    class Exploding(FakeChat):
        def chat(self, *a, **k):  # noqa: ANN001
            raise AssertionError("没有条款时不应该调用模型")

    result = extract_fields([], client=Exploding(content="{}"))
    assert result.filled == 0
    assert len(result.drafts) == 12
    assert "没有召回条款" in result.error


def test_extract_handles_llm_error() -> None:
    class Failing(FakeChat):
        def chat(self, *a, **k):  # noqa: ANN001
            raise LlmError("401 未授权", status=401)

    result = extract_fields(CLAUSES, client=Failing(content=""))
    assert result.filled == 0
    assert len(result.drafts) == 12
    assert "401" in result.error


def test_extract_reports_unparseable_output() -> None:
    result = extract_fields(CLAUSES, client=FakeChat(content="我觉得总价是 128 万"))
    assert result.drafts == []
    assert result.warnings == ["模型输出无法解析"]


# ---------- 条款收集 ----------


def test_collect_clauses_dedupes_and_limits() -> None:
    def retriever(query: str, top_k: int = 6) -> list[ClauseHit]:
        # 两个查询都返回同一批，验证去重
        return CLAUSES if query else []

    collected = collect_clauses(retriever, ["付款", "总价"], top_k=6)
    assert [c.clause_id for c in collected] == ["DOC901-C0003", "DOC901-C0008"]


def test_collect_clauses_empty() -> None:
    assert collect_clauses(lambda *a, **k: [], ["x"]) == []
