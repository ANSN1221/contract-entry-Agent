"""工具层验收：白名单、参数强校验、结果形状、指标计算。

真实检索依赖项目一的索引与 Embedding key，这里用假检索器注入，
保证单测在无网络、无索引的机器上也能跑。
"""

from __future__ import annotations

import pytest

from app.adapters.rag import ClauseHit, RagUnavailable
from app.tools import ToolContext, ToolError, list_tools, run_tool, tool_names
from app.tools.search_clauses import compute_metrics, search_clauses_tool


def make_hit(
    clause_id: str = "DOC-C0012",
    *,
    text: str = "甲方应于合同签订后十日内支付合同总价的百分之三十。",
    dense_rank: int | None = 1,
    bm25_rank: int | None = 1,
    rerank_score: float | None = None,
) -> ClauseHit:
    return ClauseHit(
        clause_id=clause_id,
        text=text,
        citation_label="第 3 页 第 5.2 条",
        page_start=3,
        page_end=3,
        score=0.032,
        dense_rank=dense_rank,
        bm25_rank=bm25_rank,
        rerank_score=rerank_score,
    )


def fake_retriever(hits: list[ClauseHit], captured: dict | None = None):
    def _search(
        query: str,
        top_k: int = 8,
        *,
        use_rerank: bool | None = None,
        doc_ids: list[str] | None = None,
        **_: object,
    ):
        if captured is not None:
            captured.update(
                {"query": query, "top_k": top_k, "use_rerank": use_rerank, "doc_ids": doc_ids}
            )
        return hits[:top_k]

    return _search


# ---------- 白名单 ----------


def test_whitelist_contents() -> None:
    assert set(tool_names()) == {"search_clauses", "verify_extraction"}


def test_tool_schema_is_llm_ready() -> None:
    specs = {spec["name"]: spec for spec in list_tools()}
    assert set(specs) == {"search_clauses", "verify_extraction"}
    # function calling 需要 JSON Schema 形状的 parameters
    for spec in specs.values():
        assert spec["parameters"]["type"] == "object"
        assert spec["parameters"]["properties"]
        assert spec["description"]


def test_unknown_tool_rejected() -> None:
    with pytest.raises(ToolError) as exc:
        run_tool("delete_all_tasks", {"query": "x"})
    assert exc.value.code == "UNKNOWN_TOOL"
    assert "search_clauses" in exc.value.detail["allowed"]


# ---------- 参数强校验 ----------


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        ({}, "缺 query"),
        ({"query": ""}, "query 为空"),
        ({"query": "   "}, "query 全空白"),
        ({"query": "x" * 201}, "query 超长"),
        ({"query": "价格", "top_k": 0}, "top_k 太小"),
        ({"query": "价格", "top_k": 21}, "top_k 超上限"),
        ({"query": "价格", "extra": 1}, "多余字段"),
    ],
)
def test_invalid_args_rejected(args: dict, reason: str) -> None:
    with pytest.raises(ToolError) as exc:
        run_tool("search_clauses", args)
    assert exc.value.code == "INVALID_ARGS", reason
    assert exc.value.detail["problems"]


def test_extra_field_is_forbidden_not_ignored() -> None:
    """模型多塞字段必须报错——猜它的意思比直接失败更危险。"""
    with pytest.raises(ToolError) as exc:
        run_tool("search_clauses", {"query": "违约金", "limit": 5})
    assert exc.value.code == "INVALID_ARGS"
    assert any("limit" in p["loc"] for p in exc.value.detail["problems"])


def test_query_is_stripped() -> None:
    captured: dict = {}
    run_tool(
        "search_clauses",
        {"query": "  履约保证金  "},
        context=ToolContext(retriever=fake_retriever([], captured)),
    )
    assert captured["query"] == "履约保证金"


# ---------- 正常路径 ----------


def test_search_returns_payload() -> None:
    hits = [make_hit("DOC-C0012"), make_hit("DOC-C0013", dense_rank=2, bm25_rank=None)]
    result = run_tool(
        "search_clauses",
        {"query": "付款方式", "top_k": 2},
        context=ToolContext(retriever=fake_retriever(hits), task_id="task-1"),
    )

    assert result["count"] == 2
    assert result["query"] == "付款方式"
    first = result["items"][0]
    assert first["clause_id"] == "DOC-C0012"
    assert first["citation"] == "第 3 页 第 5.2 条"
    assert first["page"] == "3-3"
    assert first["preview"]
    assert first["dense_rank"] == 1


def test_preview_is_truncated() -> None:
    long_text = "条款正文" * 100
    hits = [make_hit(text=long_text)]
    result = search_clauses_tool("长条款", context=ToolContext(retriever=fake_retriever(hits)))
    preview = result["items"][0]["preview"]
    assert len(preview) <= 141  # 140 字 + 省略号
    assert preview.endswith("…")


def test_empty_result_is_not_an_error() -> None:
    result = run_tool(
        "search_clauses",
        {"query": "不存在的条款"},
        context=ToolContext(retriever=fake_retriever([])),
    )
    assert result["count"] == 0
    assert result["items"] == []
    assert "换个说法" in result["hint"]


def test_hint_tells_model_to_read_original() -> None:
    result = run_tool(
        "search_clauses",
        {"query": "违约责任"},
        context=ToolContext(retriever=fake_retriever([make_hit()])),
    )
    assert "read_clause" in result["hint"]


# ---------- 指标 ----------


def test_metrics_count_dual_hits() -> None:
    hits = [
        make_hit("A", dense_rank=1, bm25_rank=2),
        make_hit("B", dense_rank=2, bm25_rank=None),
        make_hit("C", dense_rank=None, bm25_rank=1),
    ]
    metrics = compute_metrics(hits)
    assert metrics.recalled == 3
    assert metrics.dense_hit == 2
    assert metrics.bm25_hit == 2
    assert metrics.dual_hit == 1
    assert metrics.rerank_applied is False
    assert metrics.top_clause_ids == ["A", "B", "C"]


def test_metrics_mark_rerank() -> None:
    hits = [make_hit("A", rerank_score=0.81)]
    metrics = compute_metrics(hits)
    assert metrics.rerank_applied is True
    assert metrics.as_dict()["rerank_applied"] is True


def test_metrics_present_in_result() -> None:
    result = run_tool(
        "search_clauses",
        {"query": "质保期"},
        context=ToolContext(retriever=fake_retriever([make_hit()])),
    )
    assert result["metrics"]["recalled"] == 1
    assert result["metrics"]["dual_hit"] == 1


# ---------- 失败翻译 ----------


def test_rag_unavailable_becomes_tool_error() -> None:
    def broken(*_: object, **__: object) -> list[ClauseHit]:
        raise RagUnavailable("contract-qa 未安装")

    with pytest.raises(ToolError) as exc:
        run_tool("search_clauses", {"query": "验收"}, context=ToolContext(retriever=broken))
    assert exc.value.code == "RAG_UNAVAILABLE"
    assert "未安装" in exc.value.message


def test_unexpected_exception_wrapped() -> None:
    def boom(*_: object, **__: object) -> list[ClauseHit]:
        raise RuntimeError("向量库连接断了")

    with pytest.raises(ToolError) as exc:
        run_tool("search_clauses", {"query": "验收"}, context=ToolContext(retriever=boom))
    assert exc.value.code == "TOOL_EXECUTION_FAILED"
    assert "RuntimeError" in exc.value.message


def test_default_retriever_translates_missing_package(monkeypatch) -> None:  # noqa: ANN001
    """未装 contract-qa 时，默认检索器要把部署问题翻译成 RAG_UNAVAILABLE。"""

    def _missing(*_: object, **__: object) -> list[ClauseHit]:
        raise RagUnavailable("contract-qa 未安装")

    # 真实环境可能已安装并建好索引，这里强制模拟缺包
    monkeypatch.setattr("app.adapters.rag._retriever", _missing)
    with pytest.raises(ToolError) as exc:
        run_tool("search_clauses", {"query": "付款"})
    assert exc.value.code == "RAG_UNAVAILABLE"


# ---------- 文档范围限定 ----------


def test_doc_ids_passed_to_retriever() -> None:
    """多合同语料下必须把召回限定在本合同内，参数要原样传下去。"""
    captured: dict = {}
    run_tool(
        "search_clauses",
        {"query": "合同总价", "doc_ids": ["DOC012"]},
        context=ToolContext(retriever=fake_retriever([make_hit()], captured)),
    )
    assert captured["doc_ids"] == ["DOC012"]


def test_doc_ids_omitted_when_not_given() -> None:
    captured: dict = {}
    run_tool(
        "search_clauses",
        {"query": "合同总价"},
        context=ToolContext(retriever=fake_retriever([], captured)),
    )
    assert "doc_ids" not in captured or captured["doc_ids"] is None


def test_too_many_doc_ids_rejected() -> None:
    with pytest.raises(ToolError) as exc:
        run_tool("search_clauses", {"query": "x", "doc_ids": [f"D{i}" for i in range(11)]})
    assert exc.value.code == "INVALID_ARGS"


def test_result_echoes_doc_scope() -> None:
    result = run_tool(
        "search_clauses",
        {"query": "付款", "doc_ids": ["DOC004"]},
        context=ToolContext(retriever=fake_retriever([make_hit()])),
    )
    assert result["doc_ids"] == ["DOC004"]
