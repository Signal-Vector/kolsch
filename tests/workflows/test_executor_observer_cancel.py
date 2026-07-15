"""spec-011 — IterativeExecutor observer + cancel plumbing.

Four tests covering the executor's new `event_observer` and
`cancel_token` kwargs:

- Observer receives every emitted `WorkflowEvent` after the store
  append (durable-first).
- Observer exceptions are caught + logged; the run completes normally.
- The workflow's `cancel_token` reaches sub-agent invocations via
  `NodeContext.cancel_token`, and the runner composes it into
  `ExecutionContext.cancellation` alongside any exchange-scoped token
  through `CombinedCancelToken`.
- The composition preserves the exchange token — either firing side
  triggers cancellation.

The tests use synthetic runners that mimic the sub-agent handshake
(read `ctx.cancel_token`, build an `ExecutionContext`, compose) so
the AgentRunner + engine machinery doesn't need to be spun up.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from assets.catalog import AssetCatalog
from engine.context import CancelToken, CombinedCancelToken, ExecutionContext
from schemas import ThreadId
from workflows.events import WorkflowEvent
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run_store import WorkflowRunStore

# ---------------------------------------------------------------------------
# Test helpers.
# ---------------------------------------------------------------------------


class StaticRunner:
    """Return a fixed output dict on every invocation."""

    def __init__(self, output: dict[str, Any] | None = None) -> None:
        self._output = dict(output) if output is not None else {}

    async def run(
        self,
        *,
        node_name: str,
        node_def: Any,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: Any,
    ) -> dict[str, Any]:
        _ = (node_name, node_def, bound_inputs, state, ctx)
        return dict(self._output)


class SubAgentishRunner:
    """Runner mimicking the AgentRunner's sub-agent invocation shape.

    Reads `ctx.cancel_token` off the NodeContext, builds a fresh
    `ExecutionContext`, composes the cancel token via
    `CombinedCancelToken` (identical to `AgentRunner.run`), then waits
    for the composed token to fire (or, if it never does, sleeps a
    bounded amount and returns).

    The `captured_ctx` attribute exposes the built `ExecutionContext`
    so tests can introspect its `cancellation` type and the
    `workflow_run_id` / `workflow_node_name` stamping.
    """

    def __init__(
        self,
        *,
        thread_id: str | None = None,
        exchange_token: CancelToken | None = None,
        max_wait_seconds: float = 5.0,
    ) -> None:
        import uuid

        self._thread_id = thread_id or f"th_{uuid.uuid4()}"
        self._exchange_token = exchange_token
        self._max_wait_seconds = max_wait_seconds
        self.captured_ctx: ExecutionContext | None = None

    async def run(
        self,
        *,
        node_name: str,
        node_def: Any,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: Any,
    ) -> dict[str, Any]:
        _ = (node_def, bound_inputs, state)
        exec_ctx = ExecutionContext(
            thread_id=ThreadId(self._thread_id),
            trigger={"kind": "invocation", "inputs": {}},
            cancellation=self._exchange_token,
            workflow_run_id=None,
            workflow_node_name=None,
        )
        # Mirror `AgentRunner.run` — stamp workflow context + compose
        # the cancel token when the NodeContext carries one.
        exec_ctx.workflow_node_name = node_name
        if ctx.workflow_run_id is not None:
            from workflows.ids import WorkflowRunId

            exec_ctx.workflow_run_id = WorkflowRunId(ctx.workflow_run_id)
        if ctx.cancel_token is not None:
            exec_ctx.cancellation = CombinedCancelToken(
                ctx.cancel_token,
                exec_ctx.cancellation or CancelToken(),
            )
        self.captured_ctx = exec_ctx

        # Poll the composed token like an LLM stream would — return as
        # soon as it fires. Bounded to keep tests fast on failure.
        deadline = asyncio.get_running_loop().time() + self._max_wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            if (
                exec_ctx.cancellation is not None
                and exec_ctx.cancellation.is_cancelled()
            ):
                break
            await asyncio.sleep(0.01)
        return {
            "cancelled": bool(
                exec_ctx.cancellation is not None
                and exec_ctx.cancellation.is_cancelled()
            )
        }


def _make_workflow(nodes: dict[str, Any]) -> Workflow:
    """Build a linear workflow through `nodes` in insertion order."""
    node_names = list(nodes.keys())
    edges = [
        {"from": node_names[i], "to": node_names[i + 1]}
        for i in range(len(node_names) - 1)
    ]
    return Workflow.model_validate({"nodes": nodes, "graph": {"edges": edges}})


def _make_executor(
    tmp_path: Path,
    *,
    workflow: Workflow,
    registry: RunnerRegistry,
    event_observer: Any = None,
    cancel_token: CancelToken | None = None,
) -> tuple[IterativeExecutor, WorkflowRunStore, Any]:
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=AssetCatalog(),
        event_observer=event_observer,
        cancel_token=cancel_token,
    )
    return ex, store, run


# ---------------------------------------------------------------------------
# Test 1 — Observer receives every event (durable-first).
# ---------------------------------------------------------------------------


class TestObserverReceives:
    async def test_observer_receives(self, tmp_path: Path) -> None:
        wf = _make_workflow(
            {
                "first": {"type": "kolsch/nodes/noop"},
                "second": {"type": "kolsch/nodes/noop"},
                "third": {"type": "kolsch/nodes/noop"},
            }
        )
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", StaticRunner())

        received: list[WorkflowEvent] = []
        ex, store, run = _make_executor(
            tmp_path,
            workflow=wf,
            registry=registry,
            event_observer=received.append,
        )

        from workflows.run import WorkflowRunStatus

        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED

        # Observer sees every event in the JSONL, same order.
        events_from_disk = list(store.read_events(run.run_id))
        received_kinds = [e.kind.value for e in received]
        disk_kinds = [e["kind"] for e in events_from_disk]
        assert received_kinds == disk_kinds

        # Framing + at least one per-node started/completed pair.
        assert received_kinds[0] == "workflow.run.started"
        assert received_kinds[-1] == "workflow.run.completed"
        assert "workflow.node.started" in received_kinds
        assert "workflow.node.completed" in received_kinds

    async def test_observer_none_runs_identically(self, tmp_path: Path) -> None:
        wf = _make_workflow(
            {
                "first": {"type": "kolsch/nodes/noop"},
                "second": {"type": "kolsch/nodes/noop"},
            }
        )
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", StaticRunner())

        ex, store, run = _make_executor(
            tmp_path,
            workflow=wf,
            registry=registry,
            event_observer=None,
        )
        from workflows.run import WorkflowRunStatus

        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        events = list(store.read_events(run.run_id))
        # Baseline: run.started, per-node started/completed pairs,
        # run.completed. Two nodes → 1 + 2*2 + 1 = 6 events.
        assert len(events) == 6


# ---------------------------------------------------------------------------
# Test 2 — Observer exception caught + logged.
# ---------------------------------------------------------------------------


class TestObserverExceptionCaught:
    async def test_observer_exception_caught(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        wf = _make_workflow(
            {
                "only": {"type": "kolsch/nodes/noop"},
            }
        )
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", StaticRunner())

        def bad_observer(_event: WorkflowEvent) -> None:
            raise RuntimeError("bad")

        ex, store, run = _make_executor(
            tmp_path,
            workflow=wf,
            registry=registry,
            event_observer=bad_observer,
        )

        from workflows.run import WorkflowRunStatus

        with caplog.at_level(logging.ERROR, logger="workflows.executor"):
            status = await ex.run()
        # The run completed normally — a raising observer does NOT
        # kill the executor.
        assert status == WorkflowRunStatus.SUCCEEDED
        events = list(store.read_events(run.run_id))
        # Every event was still appended to disk (durable-first).
        assert events[0]["kind"] == "workflow.run.started"
        assert events[-1]["kind"] == "workflow.run.completed"
        # `logger.exception` fired: the log record carries the
        # "event_observer raised" prefix; the RuntimeError shows up
        # in the exc_info payload (checked via `record.exc_info`).
        assert any(
            "event_observer raised" in record.getMessage()
            and record.exc_info is not None
            and record.exc_info[0] is RuntimeError
            for record in caplog.records
        )


# ---------------------------------------------------------------------------
# Test 3 — Workflow cancel token reaches sub-agent.
# ---------------------------------------------------------------------------


class TestCancelTokenReachesSubagent:
    async def test_cancel_token_reaches_subagent(self, tmp_path: Path) -> None:
        wf = _make_workflow(
            {
                "sub": {"type": "kolsch/nodes/noop"},
            }
        )
        registry = RunnerRegistry()
        sub_runner = SubAgentishRunner(max_wait_seconds=5.0)
        registry.register("kolsch/nodes/noop", sub_runner)

        workflow_token = CancelToken()
        ex, _store, _run = _make_executor(
            tmp_path,
            workflow=wf,
            registry=registry,
            cancel_token=workflow_token,
        )

        async def _fire_cancel_soon() -> None:
            await asyncio.sleep(0.05)
            workflow_token.cancel()

        cancel_task = asyncio.create_task(_fire_cancel_soon())
        try:
            # `asyncio.wait_for` bounds the whole run at 2s; if
            # composition didn't work the runner would sleep for its
            # 5s max and this would time out.
            result = await asyncio.wait_for(ex.run(), timeout=2.0)
        finally:
            cancel_task.cancel()
            try:
                await cancel_task
            except (asyncio.CancelledError, Exception):
                pass

        # Run finished; the runner observed the cancel.
        assert result is not None
        # Introspection: the composed cancellation is a
        # CombinedCancelToken instance.
        assert sub_runner.captured_ctx is not None
        assert isinstance(sub_runner.captured_ctx.cancellation, CombinedCancelToken)
        # And the workflow context was stamped (r-review r1).
        assert sub_runner.captured_ctx.workflow_run_id is not None
        assert sub_runner.captured_ctx.workflow_node_name == "sub"


# ---------------------------------------------------------------------------
# Test 4 — Composition preserves the exchange token.
# ---------------------------------------------------------------------------


class TestCompositionPreservesExchange:
    async def test_exchange_token_fires_triggers_cancel(self, tmp_path: Path) -> None:
        wf = _make_workflow(
            {
                "sub": {"type": "kolsch/nodes/noop"},
            }
        )
        registry = RunnerRegistry()
        exchange_token = CancelToken()
        sub_runner = SubAgentishRunner(
            exchange_token=exchange_token, max_wait_seconds=5.0
        )
        registry.register("kolsch/nodes/noop", sub_runner)

        workflow_token = CancelToken()  # never fired in this case
        ex, _store, _run = _make_executor(
            tmp_path,
            workflow=wf,
            registry=registry,
            cancel_token=workflow_token,
        )

        async def _fire_exchange_soon() -> None:
            await asyncio.sleep(0.05)
            exchange_token.cancel()

        fire_task = asyncio.create_task(_fire_exchange_soon())
        try:
            await asyncio.wait_for(ex.run(), timeout=2.0)
        finally:
            fire_task.cancel()
            try:
                await fire_task
            except (asyncio.CancelledError, Exception):
                pass

        assert sub_runner.captured_ctx is not None
        assert sub_runner.captured_ctx.cancellation is not None
        assert sub_runner.captured_ctx.cancellation.is_cancelled()
        # Workflow token itself never fired.
        assert not workflow_token.is_cancelled()

    async def test_workflow_token_fires_triggers_cancel(self, tmp_path: Path) -> None:
        wf = _make_workflow(
            {
                "sub": {"type": "kolsch/nodes/noop"},
            }
        )
        registry = RunnerRegistry()
        exchange_token = CancelToken()  # never fired in this case
        sub_runner = SubAgentishRunner(
            exchange_token=exchange_token, max_wait_seconds=5.0
        )
        registry.register("kolsch/nodes/noop", sub_runner)

        workflow_token = CancelToken()
        ex, _store, _run = _make_executor(
            tmp_path,
            workflow=wf,
            registry=registry,
            cancel_token=workflow_token,
        )

        async def _fire_workflow_soon() -> None:
            await asyncio.sleep(0.05)
            workflow_token.cancel()

        fire_task = asyncio.create_task(_fire_workflow_soon())
        try:
            await asyncio.wait_for(ex.run(), timeout=2.0)
        finally:
            fire_task.cancel()
            try:
                await fire_task
            except (asyncio.CancelledError, Exception):
                pass

        assert sub_runner.captured_ctx is not None
        assert sub_runner.captured_ctx.cancellation is not None
        assert sub_runner.captured_ctx.cancellation.is_cancelled()
        # Exchange token itself never fired.
        assert not exchange_token.is_cancelled()


# ---------------------------------------------------------------------------
# Top-level test aliases so the spec's `pytest ...::test_<name>`
# invocation pattern resolves. The Tests section of the spec calls
# out each of these by function name.
# ---------------------------------------------------------------------------


async def test_observer_receives(tmp_path: Path) -> None:
    """Alias to `TestObserverReceives.test_observer_receives`."""
    await TestObserverReceives().test_observer_receives(tmp_path)


async def test_observer_exception_caught(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Alias to `TestObserverExceptionCaught.test_observer_exception_caught`."""
    await TestObserverExceptionCaught().test_observer_exception_caught(tmp_path, caplog)


async def test_cancel_token_reaches_subagent(tmp_path: Path) -> None:
    """Alias to `TestCancelTokenReachesSubagent.test_cancel_token_reaches_subagent`."""
    await TestCancelTokenReachesSubagent().test_cancel_token_reaches_subagent(tmp_path)


async def test_composition_preserves_exchange(tmp_path: Path) -> None:
    """Alias — runs both the exchange-fire and workflow-fire arms."""
    await TestCompositionPreservesExchange().test_exchange_token_fires_triggers_cancel(
        tmp_path
    )
    await TestCompositionPreservesExchange().test_workflow_token_fires_triggers_cancel(
        tmp_path
    )
