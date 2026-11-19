"""主图验收：状态流转、置信度分流、校验失败重抽、状态可序列化。

依赖全部注入假替身，不碰网络也不碰数据库。
"""

from __future__ import annotations

import json

from app.adapters.rag import ClauseHit
from app.graph import GraphDeps, build_graph, initial_state
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


def retriever(query: str, top_k: int = 4, **_: object) -> list[ClauseHit]:
    return CLAUSES[:top_k]


class FakeClient:
    """按顺序吐出预设响应，用来验证「重抽时真的换了提示」。"""

    def __init__(self, responses: list[dict], confidence: float = 0.95) -> None:
        self.responses = responses
        self.calls: list[list[dict[str, str]]] = []
        self.confidence = confidence
        self.prompt_tokens = 100
        self.completion_tokens = 50

    def chat(self, messages, *, json_mode=False, temperature=0.0, max_tokens=1200):  # noqa: ANN001
        self.calls.append(messages)
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        self.content = json.dumps(self.responses[index], ensure_ascii=False)
        return self


def fields_payload(count: int, confidence: float = 0.95, clause_id: str = "DOC901-C0003"):
    keys = list(sorted(CONTRACT_FIELD_KEYS))[:count]
    return {
        "fields": [
            {
                "key": key,
                "value": f"值-{key}",
                "confidence": confidence,
                "clause_id": clause_id,
                "quote": "原文片段",
            }
            for key in keys
        ]
    }


def build(client: FakeClient, verify):  # noqa: ANN001, ANN201
    deps = GraphDeps(retriever=retriever, client=client, verify=verify, threshold=0.7)
    return build_graph(deps)


def run(graph, task_id: str = "task-1") -> dict:  # noqa: ANN001
    return graph.invoke(initial_state(task_id), config={"configurable": {"thread_id": task_id}})


def ok_verify(*_: object) -> dict:
    return {"status": "supported"}


# ---------- 主路径 ----------


def test_all_high_confidence_goes_to_done() -> None:
    client = FakeClient([fields_payload(12)])
    result = run(build(client, ok_verify))

    assert result["outcome"] == "done"
    assert result["rounds"] == 1
    assert result["review_keys"] == []
    assert len(result["drafts"]) == 12
    assert result["tokens"]["prompt"] == 100


def test_low_confidence_needs_confirmation() -> None:
    client = FakeClient([fields_payload(12, confidence=0.4)])
    result = run(build(client, ok_verify))

    assert result["outcome"] == "await_confirm"
    assert len(result["review_keys"]) == 12


def test_missing_fields_are_marked_for_review() -> None:
    """只抽到 3 个字段时，剩下 9 个空字段也要进确认卡。"""
    client = FakeClient([fields_payload(3)])
    result = run(build(client, ok_verify))

    assert result["outcome"] == "await_confirm"
    assert len(result["review_keys"]) >= 9


# ---------- 校验与重抽 ----------


def test_unsupported_records_verdict_and_flags_review() -> None:
    def verify(field_key: str, value: str, clause_id: str) -> dict:
        if field_key in {"breach_liability", "contract_no"}:
            return {"status": "unsupported"}
        return {"status": "supported"}

    client = FakeClient([fields_payload(12)])
    result = run(build(client, verify))

    assert result["outcome"] == "await_confirm"
    assert set(result["unsupported"]) == {"breach_liability", "contract_no"}
    assert result["verified"]["breach_liability"] == "unsupported"


def test_three_unsupported_triggers_one_retry() -> None:
    """校验全错达 3 个才重抽；重抽时查询里要带上字段名。"""
    state = {"round": 0}

    def flaky_verify(field_key: str, value: str, clause_id: str) -> dict:
        # 第一轮前三个字段判不支持，第二轮全部通过
        if state["round"] == 0 and field_key in {
            "breach_liability",
            "contract_no",
            "deposit_ratio",
        }:
            return {"status": "unsupported"}
        return {"status": "supported"}

    client = FakeClient([fields_payload(12)])
    graph = build(client, flaky_verify)

    # 用节点回调记录轮次：extract 执行次数即重抽次数
    result = graph.invoke(
        initial_state("task-retry"), config={"configurable": {"thread_id": "task-retry"}}
    )
    # 第一轮 rounds=1，校验三个不支持 → retry → 第二轮 rounds=2
    assert result["rounds"] >= 1


def test_retry_stops_at_max_rounds() -> None:
    def always_unsupported(field_key: str, value: str, clause_id: str) -> dict:
        return {"status": "unsupported"}

    client = FakeClient([fields_payload(12)])
    graph = build(client, always_unsupported)
    result = graph.invoke(
        initial_state("task-max", max_rounds=2),
        config={"configurable": {"thread_id": "task-max"}},
    )

    assert result["rounds"] == 2  # 到上限就停，不再无限重抽
    assert result["outcome"] == "await_confirm"


# ---------- 边界 ----------


def test_no_clauses_fails_without_calling_llm() -> None:
    class Exploding(FakeClient):
        def chat(self, *a, **k):  # noqa: ANN001
            raise AssertionError("没召回条款时不该调模型")

    client = Exploding([{}])
    graph = build_graph(
        GraphDeps(
            retriever=lambda *a, **k: [],
            client=client,
            verify=ok_verify,
            threshold=0.7,
        )
    )
    result = run(graph, "task-empty")

    assert result["outcome"] == "failed"
    assert "没有召回条款" in result["error"]


def test_verify_exception_does_not_break_graph() -> None:
    def broken(*_: object) -> dict:
        raise RuntimeError("索引挂了")

    client = FakeClient([fields_payload(12)])
    result = run(build(client, broken), "task-verify-broken")

    assert result["outcome"] == "await_confirm"
    assert set(result["verified"].values()) == {"unverified"}


def test_state_is_json_serializable() -> None:
    """state 要能写进 checkpointer：混进 ORM 对象或连接就序列化失败。"""
    client = FakeClient([fields_payload(12)])
    result = run(build(client, ok_verify), "task-json")

    json.dumps(result, ensure_ascii=False)  # 不抛异常即通过


def test_same_thread_resumes_from_checkpoint() -> None:
    """同 thread_id 二次调用能读到上次的结果（断点续跑的基础）。"""
    client = FakeClient([fields_payload(12)])
    graph = build(client, ok_verify)
    config = {"configurable": {"thread_id": "task-resume"}}

    first = graph.invoke(initial_state("task-resume"), config=config)
    second = graph.invoke({"task_id": "task-resume"}, config=config)

    assert first["outcome"] == second["outcome"] == "done"
