"""后台执行器：把任务从队列捞出来跑图，管超时、轮次与死信。

为什么不用 Celery：单机 asyncio 已经够用，而「状态与连接解耦」的关键不是
队列实现，是**任务状态落库**——进程重启后照着重跑。README 里画了多 worker
演进图，真要多实例时换 Redis 队列即可，`run_task` 的接口不变。

三条可靠性边界：

| 场景 | 处理 |
| --- | --- |
| 超过 `runner_timeout_seconds` | 取消执行，任务转 `failed`，`error_code=TIMEOUT` |
| 图执行抛异常 | 任务转 `failed`，`error_code=RUNNER_ERROR`，堆栈进日志 |
| 抽不到任何字段 | 图自己给 `outcome=failed`，任务转 `failed` |
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, delete, select

from app.config import Settings
from app.errors import Conflict, NotFound
from app.events import EventType, emit
from app.log import get_logger
from app.models import Task, TaskField, TaskStatus, utcnow
from app.state_machine import InvalidTransition, transition
from app.store import Database

log = get_logger("app.runner")

#: `_begin` 的返回值：None 表示可以执行，DEAD 表示已进死信、不要执行
DEAD = "dead"


@dataclass(slots=True)
class RunOutcome:
    task_id: str
    status: str
    outcome: str = ""
    rounds: int = 0
    error: str = ""
    tokens: dict[str, int] = field(default_factory=dict)
    review_keys: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    confirm_token: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "outcome": self.outcome,
            "rounds": self.rounds,
            "error": self.error,
            "tokens": self.tokens,
            "review_keys": self.review_keys,
            "unsupported": self.unsupported,
            "confirm_token": self.confirm_token,
        }


class TaskRunner:
    def __init__(
        self,
        *,
        database: Database,
        graph: Any | None = None,
        settings: Settings,
        workers: int | None = None,
        graph_factory: Callable[[], Awaitable[tuple[Any, Any]]] | None = None,
    ) -> None:
        self.database = database
        self.graph = graph
        self.settings = settings
        self.workers = settings.runner_workers if workers is None else workers
        self.graph_factory = graph_factory
        self._checkpoint_conn: Any = None
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False

    async def _ensure_graph(self) -> Any:
        """图可能依赖异步资源（持久化 checkpointer 的连接），所以懒加载。

        factory 返回 `(图, 需要关闭的连接)`，连接的生命周期绑在 runner 上。
        """
        if self.graph is None:
            if self.graph_factory is None:
                raise RuntimeError("未提供 graph 或 graph_factory")
            self.graph, self._checkpoint_conn = await self.graph_factory()
        return self.graph

    # ---------- 队列 ----------

    async def start(self) -> None:
        await self._ensure_graph()
        if self.workers <= 0:
            log.info("runner_disabled")
            return
        self._stopping = False
        for index in range(self.workers):
            self._tasks.append(asyncio.create_task(self._worker(index), name=f"runner-{index}"))
        log.info("runner_started", workers=self.workers)

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # worker 内部异常已在 _worker 里记过日志
                log.warning("runner_worker_exit_with_error")
        self._tasks.clear()
        if self._checkpoint_conn is not None:
            try:
                await self._checkpoint_conn.close()
            except Exception:  # pragma: no cover - 关闭失败不该阻塞停机
                log.warning("checkpoint_close_failed")
            self._checkpoint_conn = None
        log.info("runner_stopped")

    async def submit(self, task_id: str) -> None:
        with self.database.session() as session:
            emit(session, task_id, EventType.QUEUED, {"via": "api"})
        await self._queue.put(task_id)

    async def _worker(self, index: int) -> None:
        while not self._stopping:
            task_id = await self._queue.get()
            try:
                await self.run_task(task_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # 死信：单任务失败不能把 worker 带走
                log.exception("runner_dead_letter", worker=index, task_id=task_id)
            finally:
                self._queue.task_done()

    # ---------- 执行 ----------

    async def run_task(self, task_id: str, *, resume: bool = False) -> RunOutcome:
        graph = await self._ensure_graph()
        doc_ids = self._doc_ids_for(task_id)
        if self._begin(task_id, resume=resume) == DEAD:
            return RunOutcome(
                task_id=task_id,
                status=TaskStatus.FAILED.value,
                error=f"超过最大执行次数（{self.settings.max_attempts}），已进死信",
            )

        config = {"configurable": {"thread_id": task_id}}
        try:
            result = await asyncio.wait_for(
                graph.ainvoke(
                    {"task_id": task_id} if resume else _initial(task_id, doc_ids=doc_ids),
                    config=config,
                ),
                timeout=self.settings.runner_timeout_seconds,
            )
        except TimeoutError:
            return self._fail(
                task_id, "TIMEOUT", f"执行超过 {self.settings.runner_timeout_seconds}s"
            )
        except Exception as exc:
            log.exception("runner_error", task_id=task_id)
            return self._fail(task_id, "RUNNER_ERROR", f"{type(exc).__name__}: {exc}"[:200])

        return self._finish(task_id, result)

    # ---------- 落库 ----------

    def _doc_ids_for(self, task_id: str) -> list[str] | None:
        """任务绑定的文档 ID → 召回范围。没绑定就全库检索（单合同场景）。"""
        with self.database.session() as session:
            task = session.get(Task, task_id)
            doc_id = task.doc_id if task else None
        return [doc_id] if doc_id else None

    def _begin(self, task_id: str, *, resume: bool) -> str | None:
        with self.database.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise NotFound("任务不存在", detail={"task_id": task_id})

            if task.is_terminal:
                raise Conflict(
                    code="TASK_TERMINAL",
                    message=f"任务已处于终态（{task.status}），不能再执行",
                    detail={"task_id": task_id, "status": task.status},
                )

            # 死信：反复失败的任务不再重试，转终态并留证据，避免无限占用 worker
            if task.attempt >= self.settings.max_attempts:
                task.error_code = "DEAD_LETTER"
                task.blocked_reason = (
                    f"已执行 {task.attempt} 次仍未成功，超过上限 {self.settings.max_attempts}"
                )
                task.updated_at = utcnow()
                session.add(task)
                session.flush()
                if task.status != TaskStatus.RUNNING.value:
                    transition(
                        session,
                        task,
                        TaskStatus.RUNNING,
                        reason="死信归档前校正状态",
                        actor="runner",
                    )
                transition(
                    session, task, TaskStatus.FAILED, reason="超过最大执行次数", actor="runner"
                )
                emit(
                    session,
                    task_id,
                    EventType.FAILED,
                    {"code": "DEAD_LETTER", "attempts": task.attempt},
                )
                log.warning("task_dead_letter", task_id=task_id, attempts=task.attempt)
                return DEAD

            if task.status == TaskStatus.PENDING.value:
                transition(session, task, TaskStatus.RUNNING, reason="调度器领取", actor="runner")
                emit(session, task_id, EventType.RUNNING, {"attempt": task.attempt + 1})
            elif task.status == TaskStatus.RUNNING.value and not resume:
                raise Conflict(
                    code="ALREADY_RUNNING",
                    message="任务正在执行中",
                    detail={"task_id": task_id},
                )

            task.attempt += 1
            task.updated_at = utcnow()
            session.add(task)
            session.flush()
            return None

    def _finish(self, task_id: str, result: dict[str, Any]) -> RunOutcome:
        outcome = str(result.get("outcome") or "failed")
        drafts = result.get("drafts", [])
        review_keys = list(result.get("review_keys", []))
        unsupported = list(result.get("unsupported", []))
        tokens = dict(result.get("tokens", {}))
        rounds = int(result.get("rounds", 0))
        error = str(result.get("error", "") or "")

        with self.database.session() as session:
            task = session.get(Task, task_id)
            if task is None or task.is_terminal:
                return RunOutcome(task_id=task_id, status="skipped", outcome=outcome)

            verified = dict(result.get("verified", {}))
            _persist_fields(session, task_id, drafts, verified)

            filled = sum(1 for draft in drafts if draft.get("value"))
            emit(
                session,
                task_id,
                EventType.EXTRACTED,
                {"filled": filled, "rounds": rounds, "tokens": tokens},
            )
            emit(
                session,
                task_id,
                EventType.VERIFIED,
                {
                    "supported": sum(1 for v in verified.values() if v == "supported"),
                    "unsupported": unsupported,
                    "needs_review": review_keys,
                },
            )

            task.round_count = rounds
            task.updated_at = utcnow()
            if unsupported:
                task.blocked_reason = "字段校验未通过：" + "、".join(unsupported[:5])
            session.add(task)
            session.flush()

            target = TaskStatus(outcome) if outcome in set(TaskStatus) else TaskStatus.FAILED
            if target == TaskStatus.FAILED:
                task.error_code = "EXTRACT_EMPTY"
            try:
                transition(
                    session,
                    task,
                    target,
                    reason=f"执行完成，rounds={rounds}",
                    actor="runner",
                )
            except InvalidTransition as exc:
                log.warning("runner_invalid_transition", task_id=task_id, error=str(exc))

            _emit_status_event(session, task_id, target, review_keys, error)

            confirm_token = None
            if task.status == TaskStatus.AWAIT_CONFIRM.value:
                # 一次性凭证：进确认卡时发放，确认接口用掉即作废
                if not task.confirm_token:
                    task.confirm_token = secrets.token_urlsafe(18)
                    session.add(task)
                    session.flush()
                confirm_token = task.confirm_token

            return RunOutcome(
                task_id=task_id,
                status=task.status,
                outcome=outcome,
                rounds=rounds,
                error=error,
                tokens=tokens,
                review_keys=review_keys,
                unsupported=unsupported,
                confirm_token=confirm_token,
            )

    def _fail(self, task_id: str, code: str, message: str) -> RunOutcome:
        log.warning("runner_failed", task_id=task_id, code=code, message=message)
        with self.database.session() as session:
            task = session.get(Task, task_id)
            if task is None or task.is_terminal:
                return RunOutcome(task_id=task_id, status="skipped", error=message)
            task.error_code = code
            task.blocked_reason = message
            task.updated_at = utcnow()
            session.add(task)
            session.flush()
            emit(session, task_id, EventType.FAILED, {"code": code, "message": message})
            try:
                transition(session, task, TaskStatus.FAILED, reason=message, actor="runner")
            except InvalidTransition:
                pass
            return RunOutcome(task_id=task_id, status=task.status, error=message)


def _initial(task_id: str, *, doc_ids: list[str] | None = None) -> dict[str, Any]:
    from app.graph import initial_state

    return dict(initial_state(task_id, doc_ids=doc_ids))


def _emit_status_event(
    session: Session,
    task_id: str,
    target: TaskStatus,
    review_keys: list[str],
    error: str,
) -> None:
    if target == TaskStatus.AWAIT_CONFIRM:
        emit(session, task_id, EventType.AWAIT_CONFIRM, {"needs_review": review_keys})
    elif target == TaskStatus.DONE:
        emit(session, task_id, EventType.DONE, {})
    elif target == TaskStatus.BLOCKED:
        emit(session, task_id, EventType.FAILED, {"status": "blocked"})
    else:
        emit(session, task_id, EventType.FAILED, {"error": error or "抽取失败"})


def _persist_fields(
    session: Session,
    task_id: str,
    drafts: list[dict[str, Any]],
    verified: dict[str, str],
) -> None:
    """字段结果整体覆盖：重跑时不留上一轮的残值。"""
    session.exec(delete(TaskField).where(TaskField.task_id == task_id))
    for draft in drafts:
        key = str(draft.get("field_key", ""))
        status = verified.get(key)
        session.add(
            TaskField(
                task_id=task_id,
                field_key=key,
                value=draft.get("value"),
                confidence=float(draft.get("confidence") or 0.0),
                needs_review=bool(draft.get("needs_review")),
                source_clause_id=draft.get("clause_id"),
                evidence=draft.get("quote"),
                verified=None if status is None else status == "supported",
                verify_note=status,
            )
        )
    session.flush()


def load_fields(session: Session, task_id: str) -> list[TaskField]:
    stmt = select(TaskField).where(TaskField.task_id == task_id).order_by(TaskField.id)
    return list(session.exec(stmt))


def build_runner(settings: Settings, database: Database) -> TaskRunner:
    """按配置装配真实依赖：检索器、模型、校验工具。

    图不在构造时建，因为持久化 checkpointer 要先建立异步连接；
    交给 `graph_factory`，由 `TaskRunner.start()` 在事件循环里完成。
    """
    from app.adapters.rag import get_clause, search_clauses
    from app.graph import GraphDeps, build_graph, make_async_checkpointer
    from app.llm import LlmClient
    from app.tools import run_tool

    client = LlmClient(settings)

    def verify(field_key: str, value: str, clause_id: str) -> dict[str, Any]:
        from app.tools import ToolContext

        return run_tool(
            "verify_extraction",
            {"field_key": field_key, "value": value, "clause_id": clause_id},
            context=ToolContext(extras={"get_clause": get_clause}),
        )

    deps = GraphDeps(
        retriever=search_clauses,
        client=client,
        verify=verify,
        threshold=settings.extract_confidence_threshold,
    )

    holder: dict[str, Any] = {}

    async def graph_factory() -> tuple[Any, Any]:
        checkpointer, conn = await make_async_checkpointer(settings.checkpoint_path)
        holder["conn"] = conn
        return build_graph(deps, checkpointer=checkpointer), conn

    return TaskRunner(
        database=database,
        settings=settings,
        graph_factory=graph_factory,
    )
