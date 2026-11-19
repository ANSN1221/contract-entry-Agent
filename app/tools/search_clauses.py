"""合同条款检索工具：把项目一的二路召回包成 Agent 可调用的工具。

工具层只做三件事，别的都交给下游：

- 参数强校验（query 不许空、top_k 有上限）
- 调用适配层拿条款，把 `ClauseHit` 压成「模型够用」的形状
- 记检索质量指标（召回条数、双路命中分布、重排前后名次）

为什么结果里带 `preview` 而不是全文：模型第一次搜索只需要判断「这条值不值得细读」，
给 140 字够用；要原文再调 `read_clause`（D5）。省 token 也是工程决策。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.adapters.rag import ClauseHit, RagUnavailable
from app.adapters.rag import search_clauses as default_search
from app.log import get_logger
from app.tools import ToolContext, ToolError, ToolSpec

log = get_logger("app.tools.search_clauses")

PREVIEW_CHARS = 140
MAX_TOP_K = 20


class SearchClausesArgs(BaseModel):
    """参数契约。`extra="forbid"` 是刻意的：模型多塞字段必须报错。"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=200, description="检索词，保留合同原文术语效果最好")
    top_k: int = Field(default=8, ge=1, le=MAX_TOP_K, description="返回条款条数")
    use_rerank: bool | None = Field(default=None, description="是否启用重排；None 表示跟随配置")
    doc_ids: list[str] | None = Field(
        default=None,
        max_length=10,
        description='限定召回文档，如 ["DOC012"]；不传则全库检索',
    )

    @field_validator("query")
    @classmethod
    def _strip_query(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("query 不能只有空白")
        return cleaned


@dataclass(slots=True)
class SearchMetrics:
    """检索质量指标。D9 接 /metrics 时直接从这里读，不用重新算。"""

    recalled: int
    dense_hit: int
    bm25_hit: int
    dual_hit: int
    rerank_applied: bool
    top_clause_ids: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "recalled": self.recalled,
            "dense_hit": self.dense_hit,
            "bm25_hit": self.bm25_hit,
            "dual_hit": self.dual_hit,
            "rerank_applied": self.rerank_applied,
            "top_clause_ids": self.top_clause_ids,
        }


def compute_metrics(hits: list[ClauseHit]) -> SearchMetrics:
    """双路命中分布：只看「词命中/向量命中/两路都中」，能直接看出检索偏科。"""
    dense = sum(1 for hit in hits if hit.dense_rank is not None)
    bm25 = sum(1 for hit in hits if hit.bm25_rank is not None)
    dual = sum(1 for hit in hits if hit.dense_rank is not None and hit.bm25_rank is not None)
    return SearchMetrics(
        recalled=len(hits),
        dense_hit=dense,
        bm25_hit=bm25,
        dual_hit=dual,
        rerank_applied=any(hit.rerank_score is not None for hit in hits),
        top_clause_ids=[hit.clause_id for hit in hits],
    )


def to_payload(hit: ClauseHit) -> dict[str, Any]:
    return {
        "clause_id": hit.clause_id,
        "citation": hit.citation_label,
        "page": f"{hit.page_start}-{hit.page_end}",
        "preview": hit.preview,
        "score": round(hit.score, 6),
        "dense_rank": hit.dense_rank,
        "bm25_rank": hit.bm25_rank,
        "rerank_score": hit.rerank_score,
    }


def handle(args: SearchClausesArgs, context: ToolContext) -> dict[str, Any]:
    retriever = context.retriever or default_search

    try:
        hits = retriever(
            args.query,
            top_k=args.top_k,
            use_rerank=args.use_rerank,
            doc_ids=args.doc_ids,
        )
    except RagUnavailable as exc:
        # 缺包 / 缺索引属于部署问题：不可重试，直接转 blocked 更合适
        raise ToolError("RAG_UNAVAILABLE", str(exc)) from exc
    except ValueError as exc:
        raise ToolError("INVALID_ARGS", str(exc)) from exc

    metrics = compute_metrics(hits)
    log.info("search_clauses", query=args.query, **metrics.as_dict())

    return {
        "query": args.query,
        "doc_ids": args.doc_ids,
        "count": len(hits),
        "items": [to_payload(hit) for hit in hits],
        "metrics": metrics.as_dict(),
        "hint": (
            "preview 只有摘要；判断字段值必须用 read_clause 读原文"
            if hits
            else "未召回条款，换个说法再搜；仍为空说明合同里没有对应条款"
        ),
    }


SPEC = ToolSpec(
    name="search_clauses",
    description=(
        "按关键词或语义检索合同条款，返回条款 ID、出处页码与摘要。"
        "可多次调用，换不同说法再搜。返回的是摘要，核对原文请用 read_clause。"
    ),
    args_model=SearchClausesArgs,
    handler=handle,
)


def search_clauses_tool(
    query: str,
    top_k: int = 8,
    *,
    use_rerank: bool | None = None,
    context: ToolContext | None = None,
) -> dict[str, Any]:
    """给非编排代码直接调用的便捷入口，仍走同一套校验。"""
    args = SearchClausesArgs(query=query, top_k=top_k, use_rerank=use_rerank)
    return handle(args, context or ToolContext())
