"""真实模型联调测试：默认不跑，需要显式开关。

跑法::

    RUN_LIVE_LLM=1 uv run pytest tests/test_extract_live.py -v

为什么要单独一个文件：真实调用花钱且依赖网络，放在默认测试集里会让
「跑一遍测试」变成不可控操作。默认跳过，需要时手动开。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.adapters.rag import search_clauses
from app.config import get_settings
from app.extract import collect_clauses, extract_fields
from app.llm import LlmClient
from app.tools import run_tool

pytestmark = [
    pytest.mark.skipif(os.environ.get("RUN_LIVE_LLM") != "1", reason="未开启 RUN_LIVE_LLM=1"),
    pytest.mark.skipif(
        not Path("../Contract-qa-RAG/data/processed/bm25.json.gz").exists(),
        reason="项目一索引未构建",
    ),
]

QUERIES = ["合同编号", "甲方 乙方", "合同总价", "付款方式", "违约责任", "争议解决"]


@pytest.fixture(scope="module")
def settings():  # noqa: ANN201
    s = get_settings()
    if not s.deepseek_api_key:
        pytest.skip("未配置 DEEPSEEK_API_KEY")
    return s


def test_live_extraction_and_verification(settings) -> None:  # noqa: ANN001
    clauses = collect_clauses(search_clauses, QUERIES, top_k=4)
    assert clauses, "召回为空，检查索引"

    with LlmClient(settings) as client:
        result = extract_fields(clauses, client=client, threshold=0.7)

    assert result.error == ""
    assert result.filled >= 6, f"填充字段过少：{result.filled}"

    # 抽到值就必须带得出处，且出处要能通过规则层校验
    checked = 0
    for draft in result.drafts:
        if not draft.value or not draft.clause_id:
            continue
        verdict = run_tool(
            "verify_extraction",
            {
                "field_key": draft.field_key,
                "value": draft.value,
                "clause_id": draft.clause_id,
            },
        )
        assert verdict["status"] != "unsupported", f"{draft.field_key} 被原文否定：{verdict}"
        checked += 1

    assert checked >= 6
