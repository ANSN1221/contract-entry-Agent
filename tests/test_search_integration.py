"""集成测试：打真实索引，验证适配层与工具层串起来能用。

机器上没装 `contract-qa` 或没建索引时整体跳过——CI 不该因为缺密钥变红。
本地跑法::

    uv sync --extra rag
    uv run contract-qa parse && uv run contract-qa index
"""

from __future__ import annotations

from pathlib import Path

import pytest

BM25_INDEX = Path("../Contract-qa-RAG/data/processed/bm25.json.gz")

pytestmark = pytest.mark.skipif(
    not BM25_INDEX.exists(), reason="项目一索引未构建（需 --extra rag 并运行 contract-qa index）"
)


def _tool():
    try:
        from app.tools import run_tool
    except ModuleNotFoundError:  # pragma: no cover - 依赖缺失时跳过
        pytest.skip("未安装 contract-qa")
    return run_tool


@pytest.fixture(scope="module")
def run_tool():  # noqa: ANN201
    return _tool()


def test_real_search_hits_payment_clause(run_tool) -> None:  # noqa: ANN001
    result = run_tool("search_clauses", {"query": "付款方式", "top_k": 3})

    assert result["count"] > 0
    top = result["items"][0]
    assert top["citation"]
    assert top["page"]
    # 命中的第一条应当是付款条款，且摘要里能看到关键词
    assert "付款" in top["preview"]


def test_real_search_reports_metrics(run_tool) -> None:  # noqa: ANN001
    metrics = run_tool("search_clauses", {"query": "违约金", "top_k": 5})["metrics"]
    assert metrics["recalled"] > 0
    assert metrics["dense_hit"] > 0
    assert metrics["top_clause_ids"]


def test_real_search_on_missing_topic_returns_empty_or_weak(run_tool) -> None:  # noqa: ANN001
    # 合成样例里没有「项目经理手机号」这类信息，工具不能凭空造
    result = run_tool("search_clauses", {"query": "项目经理手机号码", "top_k": 3})
    for item in result["items"]:
        assert "手机" not in item["preview"]
