"""证据校验验收：规则层、模型层、三态语义、失败翻译。"""

from __future__ import annotations

import pytest

from app.adapters.rag import ClauseHit, RagUnavailable
from app.llm import LlmError
from app.tools import ToolContext, ToolError, run_tool
from app.tools.verify_extraction import normalize, rule_check

CLAUSE_TEXT = (
    "第十一条 违约责任 乙方逾期交付的，每逾期一日按合同总价的百分之零点零五向甲方支付违约金，"
    "累计不超过合同总价的百分之十。"
)
AMOUNT_TEXT = "本合同总价为人民币壹佰贰拾捌万元整（¥1,280,000.00 元），已包含税费。"


def make_clause(clause_id: str = "DOC901-C0012", text: str = CLAUSE_TEXT) -> ClauseHit:
    return ClauseHit(
        clause_id=clause_id,
        text=text,
        citation_label="第 2 页 第十一条",
        page_start=2,
        page_end=2,
        score=1.0,
    )


_MISSING = object()


def _no_network_judge(*_: object) -> dict:
    raise AssertionError("测试不应调用真实模型，请显式传 judge")


def ctx(*, judge=None, clause=_MISSING, verify_with_llm: bool | None = None):  # noqa: ANN001
    # 默认给「找不到条款」以外的固定条款；judge 默认是个会炸的桩，
    # 防止哪条用例忘了传替身就真打网络。
    target = make_clause() if clause is _MISSING else clause
    extras: dict = {"get_clause": lambda _cid: target}
    extras["judge"] = judge or _no_network_judge
    if verify_with_llm is not None:
        extras["verify_with_llm"] = verify_with_llm
    return ToolContext(extras=extras)


def verify(args: dict, **kwargs) -> dict:
    return run_tool("verify_extraction", args, context=ctx(**kwargs))


# ---------- 归一化与规则层 ----------


def test_normalize_handles_fullwidth_and_commas() -> None:
    assert normalize("１，２８０，０００．００") == "1280000.00"
    assert normalize("甲 方\n公司") == "甲方公司"


@pytest.mark.parametrize(
    ("value", "text", "expected"),
    [
        ("1280000.00", AMOUNT_TEXT, True),
        ("1,280,000.00", AMOUNT_TEXT, True),
        ("江南智造科技有限公司", "甲方（采购方）：江南智造科技有限公司", True),
        ("一千二百八十万", AMOUNT_TEXT, False),
        ("", AMOUNT_TEXT, False),
    ],
)
def test_rule_check(value: str, text: str, expected: bool) -> None:
    assert rule_check(value, text) is expected


def test_rule_hit_skips_llm() -> None:
    """规则层命中就不该再花钱调模型。"""

    def boom(*_: object, **__: object) -> dict:
        raise AssertionError("规则命中时不应调用模型")

    result = verify(
        {"field_key": "total_amount", "value": "1280000.00", "clause_id": "DOC901-C0003"},
        judge=boom,
        clause=make_clause("DOC901-C0003", AMOUNT_TEXT),
    )
    assert result["status"] == "supported"
    assert result["method"] == "rule"
    assert result["supported"] is True
    assert result["page"] == "2-2"


# ---------- 模型层 ----------


def test_rule_miss_uses_judge_supported() -> None:
    result = verify(
        {"field_key": "deposit_ratio", "value": "5", "clause_id": "DOC901-C0012"},
        judge=lambda *_: {"supported": True, "reason": "原文「百分之五」即 5%"},
    )
    assert result["status"] == "supported"
    assert result["method"] == "llm"
    assert "5%" in result["reason"]


def test_judge_says_unsupported() -> None:
    result = verify(
        {"field_key": "deposit_ratio", "value": "10", "clause_id": "DOC901-C0012"},
        judge=lambda *_: {"supported": False, "reason": "原文是百分之五"},
    )
    assert result["status"] == "unsupported"
    assert result["supported"] is False


def test_judge_failure_degrades_to_unverified() -> None:
    """复核失败不等于造假：结论必须是未验证，交人工，而不是默认通过。"""

    def broken(*_: object) -> dict:
        raise LlmError("模型超时", retryable=True)

    result = verify(
        {"field_key": "deposit_ratio", "value": "5", "clause_id": "DOC901-C0012"},
        judge=broken,
    )
    assert result["status"] == "unverified"
    assert result["supported"] is False
    assert "模型复核失败" in result["reason"]


def test_llm_disabled_returns_unverified() -> None:
    result = verify(
        {"field_key": "deposit_ratio", "value": "5", "clause_id": "DOC901-C0012"},
        verify_with_llm=False,
    )
    assert result["status"] == "unverified"
    assert result["method"] == "none"


# ---------- 参数与失败 ----------


def test_unknown_field_rejected() -> None:
    with pytest.raises(ToolError) as exc:
        verify({"field_key": "project_manager_phone", "value": "1", "clause_id": "X"})
    assert exc.value.code == "UNKNOWN_FIELD"


def test_missing_arg_rejected() -> None:
    with pytest.raises(ToolError) as exc:
        verify({"field_key": "total_amount", "value": "1"})
    assert exc.value.code == "INVALID_ARGS"


def test_extra_arg_forbidden() -> None:
    with pytest.raises(ToolError) as exc:
        verify({"field_key": "total_amount", "value": "1", "clause_id": "X", "note": "多余"})
    assert exc.value.code == "INVALID_ARGS"


def test_clause_not_found() -> None:
    with pytest.raises(ToolError) as exc:
        verify(
            {"field_key": "total_amount", "value": "1", "clause_id": "DOC901-C9999"},
            clause=None,
        )
    assert exc.value.code == "CLAUSE_NOT_FOUND"


def test_rag_unavailable_translated() -> None:
    def broken(*_: object) -> ClauseHit | None:
        raise RagUnavailable("索引缺失")

    context = ToolContext(extras={"get_clause": broken})
    with pytest.raises(ToolError) as exc:
        run_tool(
            "verify_extraction",
            {"field_key": "total_amount", "value": "1", "clause_id": "X"},
            context=context,
        )
    assert exc.value.code == "RAG_UNAVAILABLE"
