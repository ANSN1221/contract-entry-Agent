"""外部能力适配层：把第三方包关在边界里，业务代码只认本项目的类型。

这一层的意义是**依赖隔离**：contract_qa 的 Hit / ClauseRecord 属于项目一的内部
数据结构，直接让 tools / graph 依赖它，换版本就会连锁改动。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.log import get_logger

log = get_logger("app.adapters.rag")

_INSTALL_HINT = (
    "contract-qa 未安装。先确认同级仓库存在："
    "`git clone https://github.com/ANSN1221/Contract-qa-RAG.git ../Contract-qa-RAG`，"
    "再执行 `uv sync --extra rag`。"
)


class RagUnavailable(RuntimeError):
    """检索能力不可用（未安装、索引未构建、配置缺失）。"""


@dataclass(frozen=True, slots=True)
class ClauseHit:
    """条款召回结果——本项目对外只暴露这一种形状。"""

    clause_id: str
    text: str
    citation_label: str
    page_start: int
    page_end: int
    score: float
    dense_rank: int | None = None
    bm25_rank: int | None = None
    rerank_score: float | None = None

    @property
    def preview(self) -> str:
        text = self.text.replace("\n", " ").strip()
        return text[:140] + ("…" if len(text) > 140 else "")


def _retriever(settings: Settings | None = None):  # noqa: ANN202
    try:
        from contract_qa.retrieval import get_retriever
    except ModuleNotFoundError as exc:  # 包没装
        raise RagUnavailable(_INSTALL_HINT) from exc
    return get_retriever()


def search_clauses(
    query: str,
    top_k: int = 8,
    *,
    candidates: int = 50,
    use_rerank: bool | None = None,
) -> list[ClauseHit]:
    """双路召回（BM25 + 向量，RRF 融合）后返回条款。

    索引缺失 / 向量库未建的错误原样抛出：这是部署问题，不该在业务层被吞掉。
    """
    if not query.strip():
        raise ValueError("query 不能为空")

    retriever = _retriever()
    try:
        hits = retriever.search(query, top_k=top_k, candidates=candidates, use_rerank=use_rerank)
    except FileNotFoundError as exc:
        raise RagUnavailable(f"{exc}（需先在 Contract-qa-RAG 里构建索引）") from exc

    if getattr(retriever, "index_warning", ""):
        log.warning("rag_index_inconsistent", detail=retriever.index_warning)
    if getattr(retriever, "rerank_error", ""):
        log.warning("rag_rerank_degraded", detail=retriever.rerank_error)

    return [
        ClauseHit(
            clause_id=hit.clause_id,
            text=hit.record.text,
            citation_label=hit.record.citation_label,
            page_start=hit.record.page_start,
            page_end=hit.record.page_end,
            score=hit.score,
            dense_rank=hit.dense_rank,
            bm25_rank=hit.bm25_rank,
            rerank_score=hit.rerank_score,
        )
        for hit in hits
    ]
