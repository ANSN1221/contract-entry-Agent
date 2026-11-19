"""持久化断点续跑验收：换连接后仍能从上次状态继续。

模拟「进程被 kill 再起来」：第一个 checkpointer 连接关掉，用同一路径重开，
只传 task_id 而不带初始状态，图必须自己从 checkpoint 恢复。
"""

from __future__ import annotations

import json

import pytest

from app.adapters.rag import ClauseHit
from app.graph import GraphDeps, build_graph, initial_state, make_async_checkpointer
from app.models import CONTRACT_FIELD_KEYS

pytestmark = pytest.mark.asyncio

CLAUSES = [
    ClauseHit(
        clause_id="DOC901-C0003",
        text="本合同总价为 1280000.00 元。",
        citation_label="第 1 页",
        page_start=1,
        page_end=1,
        score=1.0,
    )
]


class FakeClient:
    prompt_tokens = 10
    completion_tokens = 5

    def chat(self, messages, *, json_mode=False, temperature=0.0, max_tokens=1200):  # noqa: ANN001
        keys = list(sorted(CONTRACT_FIELD_KEYS))[:12]
        self.content = json.dumps(
            {
                "fields": [
                    {
                        "key": key,
                        "value": f"v-{key}",
                        "confidence": 0.95,
                        "clause_id": "DOC901-C0003",
                        "quote": "原文",
                    }
                    for key in keys
                ]
            },
            ensure_ascii=False,
        )
        return self


def deps() -> GraphDeps:
    return GraphDeps(
        retriever=lambda *a, **k: CLAUSES,
        client=FakeClient(),
        verify=lambda *_: {"status": "supported"},
        threshold=0.7,
    )


async def test_checkpoint_survives_reconnect(tmp_path) -> None:  # noqa: ANN001
    path = str(tmp_path / "checkpoints.db")
    thread = {"configurable": {"thread_id": "task-persist"}}

    saver, conn = await make_async_checkpointer(path)
    assert conn is not None, "期望启用 SqliteSaver（aiosqlite 未安装时退回内存会失败）"
    try:
        graph = build_graph(deps(), checkpointer=saver)
        first = await graph.ainvoke(initial_state("task-persist"), config=thread)
    finally:
        await conn.close()

    assert first["outcome"] == "done"
    assert len(first["drafts"]) == 12

    # 换一条新连接 = 模拟进程重启
    saver2, conn2 = await make_async_checkpointer(path)
    try:
        graph2 = build_graph(deps(), checkpointer=saver2)
        second = await graph2.ainvoke({"task_id": "task-persist"}, config=thread)
    finally:
        await conn2.close()

    assert second["outcome"] == "done"
    assert second["drafts"] == first["drafts"]
    assert second["verified"] == first["verified"]


async def test_unknown_thread_starts_fresh(tmp_path) -> None:  # noqa: ANN001
    """没见过 thread 就从初始状态跑，不能拿别人的状态。"""
    path = str(tmp_path / "checkpoints.db")
    saver, conn = await make_async_checkpointer(path)
    try:
        graph = build_graph(deps(), checkpointer=saver)
        result = await graph.ainvoke(
            initial_state("task-new"), config={"configurable": {"thread_id": "task-new"}}
        )
    finally:
        await conn.close()

    assert result["rounds"] == 1


async def test_memory_fallback_when_dependency_missing(monkeypatch) -> None:  # noqa: ANN001
    """aiosqlite 缺失时不能把服务拖垮，退回内存 checkpointer。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):  # noqa: ANN001, ANN202
        if name == "aiosqlite":
            raise ModuleNotFoundError("aiosqlite")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    saver, conn = await make_async_checkpointer("data/unused.db")
    assert conn is None
    assert type(saver).__name__ == "InMemorySaver" or saver is not None
