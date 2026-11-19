"""LangGraph 主图：检索 → 抽取 → 校验 → 决策，校验不过则带着提示重抽一轮。

图长这样（节点少是刻意的，每一步都能在面试里讲清楚）：

    plan ──→ retrieve ──→ extract ──→ verify ──┬─→ decide
                    ↑                           │
                    └────── retry（最多一轮）────┘

三个设计点：

1. **状态可序列化**：state 里只放 dict / 基础类型，才能写进 checkpointer。
   ORM 对象、httpx 连接不许进来。
2. **依赖从外面注入**：检索器、模型客户端、校验函数都是参数。
   图本身不碰网络也不碰数据库，所以能在单测里用假替身跑完。
3. **checkpointer 用 thread_id = task_id**：重试时同一条 thread 会从
   最后一个成功的节点继续，已完成的节点不重跑。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.adapters.rag import ClauseHit
from app.extract import ChatLike, collect_clauses, extract_fields
from app.log import get_logger

log = get_logger("app.graph")

#: 首次检索用的查询。不调模型生成查询：这一层目标是「稳定可复现」，
#: 让模型自由发挥会让召回结果每次不一样，评测就没法比。
BASE_QUERIES: tuple[str, ...] = (
    "合同编号",
    "甲方 乙方 名称",
    "合同总价 金额",
    "签订日期 生效日期",
    "履行期限",
    "付款方式",
    "违约责任 违约金",
    "争议解决 仲裁",
    "履约保证金 比例",
    "质保期",
)

MAX_ROUNDS_DEFAULT = 2


class ExtractionState(TypedDict, total=False):
    task_id: str
    queries: list[str]
    clauses: list[dict[str, str]]  # [{clause_id, text}]
    drafts: list[dict[str, Any]]
    verified: dict[str, str]  # field_key -> supported / unsupported / unverified
    unsupported: list[str]
    review_keys: list[str]
    rounds: int
    max_rounds: int
    retry_hint: str
    tokens: dict[str, int]
    warnings: list[str]
    error: str
    outcome: str  # await_confirm / done / failed


@dataclass(slots=True)
class GraphDeps:
    retriever: Callable[..., list[ClauseHit]]
    client: ChatLike
    verify: Callable[[str, str, str], dict[str, Any]]
    threshold: float = 0.7
    max_rounds: int = MAX_ROUNDS_DEFAULT


def _plan(state: ExtractionState) -> ExtractionState:
    hint = state.get("retry_hint", "")
    queries = list(BASE_QUERIES)
    if hint:
        # 重抽时补一条针对性查询：把上次没抽到的字段名直接当检索词
        queries.append(hint)
    return {"queries": queries, "rounds": state.get("rounds", 0)}


def _retrieve(state: ExtractionState, deps: GraphDeps) -> ExtractionState:
    clauses = collect_clauses(deps.retriever, state.get("queries", []), top_k=4)
    return {
        "clauses": [{"clause_id": c.clause_id, "text": c.text} for c in clauses],
    }


def _extract(state: ExtractionState, deps: GraphDeps) -> ExtractionState:
    raw = state.get("clauses", [])
    clauses = [
        ClauseHit(
            clause_id=item["clause_id"],
            text=item["text"],
            citation_label="",
            page_start=0,
            page_end=0,
            score=0.0,
        )
        for item in raw
    ]
    result = extract_fields(clauses, client=deps.client, threshold=deps.threshold)
    tokens = dict(state.get("tokens", {}))
    tokens["prompt"] = tokens.get("prompt", 0) + result.prompt_tokens
    tokens["completion"] = tokens.get("completion", 0) + result.completion_tokens

    return {
        "drafts": [d.as_dict() for d in result.drafts],
        "review_keys": result.review_keys,
        "warnings": [*state.get("warnings", []), *result.warnings],
        "error": result.error or state.get("error", ""),
        "rounds": state.get("rounds", 0) + 1,
        "tokens": tokens,
    }


def _verify(state: ExtractionState, deps: GraphDeps) -> ExtractionState:
    verdicts: dict[str, str] = {}
    unsupported: list[str] = []

    for draft in state.get("drafts", []):
        value, clause_id = draft.get("value"), draft.get("clause_id")
        if not value or not clause_id:
            continue
        try:
            result = deps.verify(draft["field_key"], value, clause_id)
        except Exception as exc:  # 单个字段校验失败不能拖垮整张任务
            log.warning("verify_failed", field=draft.get("field_key"), error=str(exc))
            verdicts[draft["field_key"]] = "unverified"
            continue
        status = str(result.get("status", "unverified"))
        verdicts[draft["field_key"]] = status
        if status == "unsupported":
            unsupported.append(draft["field_key"])

    return {"verified": verdicts, "unsupported": unsupported}


def _decide(state: ExtractionState, deps: GraphDeps) -> ExtractionState:
    drafts = state.get("drafts", [])
    filled = [d for d in drafts if d.get("value")]
    if not filled:
        return {"outcome": "failed", "error": state.get("error") or "未抽到任何字段"}

    # unverified 也算「不能自动通过」：无法核对的字段必须过人眼
    unverified = any(status != "supported" for status in state.get("verified", {}).values())
    needs_human = bool(state.get("review_keys")) or bool(state.get("unsupported")) or unverified
    return {"outcome": "await_confirm" if needs_human else "done"}


def _should_retry(state: ExtractionState, deps: GraphDeps) -> str:
    """校验全错的字段才值得重抽；零散一个字段错了就交人工，别浪费调用。"""
    unsupported = state.get("unsupported", [])
    rounds = state.get("rounds", 0)
    if len(unsupported) >= 3 and rounds < state.get("max_rounds", deps.max_rounds):
        return "retry"
    return "decide"


def _retry(state: ExtractionState) -> ExtractionState:
    unsupported = state.get("unsupported", [])
    hint = " ".join(unsupported)
    log.info("extract_retry", round=state.get("rounds", 0), fields=unsupported)
    return {
        "retry_hint": hint,
        "unsupported": [],
        "verified": {},
    }


def build_graph(deps: GraphDeps, checkpointer: Any = None):  # noqa: ANN201
    """编译主图。`checkpointer` 传 SqliteSaver 即可获得断点续跑能力。"""
    graph = StateGraph(ExtractionState)
    graph.add_node("plan", _plan)
    graph.add_node("retrieve", lambda s: _retrieve(s, deps))
    graph.add_node("extract", lambda s: _extract(s, deps))
    graph.add_node("verify", lambda s: _verify(s, deps))
    graph.add_node("decide", lambda s: _decide(s, deps))
    graph.add_node("retry", _retry)

    graph.add_edge(START, "plan")
    graph.add_edge("plan", "retrieve")
    graph.add_edge("retrieve", "extract")
    graph.add_edge("extract", "verify")
    graph.add_conditional_edges(
        "verify", lambda s: _should_retry(s, deps), {"retry": "retry", "decide": "decide"}
    )
    graph.add_edge("retry", "retrieve")
    graph.add_edge("decide", END)

    return graph.compile(checkpointer=checkpointer or MemorySaver())


def make_checkpointer(db_path: str) -> Any:
    """当前用内存 checkpointer。

    为什么不是 SqliteSaver：LangGraph 的同步 SqliteSaver 不支持 `ainvoke`，
    会直接抛 NotImplementedError；对应的 `AsyncSqliteSaver` 需要 aiosqlite，
    并且要跟着应用生命周期开关连接。断点续跑（D7）时整体换过去，
    `thread_id = task_id` 的用法不变，所以这一层现在保持接口稳定。
    """
    log.info("checkpoint_backend", backend="memory", note="D7 换 AsyncSqliteSaver 持久化")
    return MemorySaver()


def initial_state(task_id: str, *, max_rounds: int = MAX_ROUNDS_DEFAULT) -> ExtractionState:
    return {
        "task_id": task_id,
        "queries": [],
        "clauses": [],
        "drafts": [],
        "verified": {},
        "unsupported": [],
        "review_keys": [],
        "rounds": 0,
        "max_rounds": max_rounds,
        "retry_hint": "",
        "tokens": {"prompt": 0, "completion": 0},
        "warnings": [],
        "error": "",
        "outcome": "",
    }
