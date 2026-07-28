"""spec-020 — `IterativeExecutor` sub-agent observer channel.

Isolated tests for the second observer callback that the executor
threads through `NodeContext.on_sub_agent_event_callback`. The
`AgentRunner` fires the callback per-event as it iterates
`AgentEngine.execute_stream(...)`; runners with no agent invocation
never touch it.

Three tests:

1. A runner that fires the callback with a synthetic `Event` reaches
   the executor's `sub_agent_event_observer` verbatim (spec-103
   approach A: with the emitting run's id first, then the owning
   `(node_name, invocation_id)` tuple).
2. When the executor is constructed without a
   `sub_agent_event_observer`, the callback still runs but the
   executor's `_emit_sub_agent_event` short-circuits — no crash,
   no side effects.
3. A raising observer must NOT propagate up into the executor run
   loop — the workflow completes normally and the exception is
   logged.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from assets.catalog import AssetCatalog
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _make_agent_message_event(*, thread_id: str) -> Event:
    """Construct a plausible session-scoped `agent.message` `Event`."""
    return Event(
        event_id=EventId(f"ev_{uuid.uuid4()}"),
        parent_event_id=None,
        thread_id=ThreadId(thread_id),
        session_id=SessionId(f"ses_{uuid.uuid4()}"),
        type=EventType.AGENT_MESSAGE,
        data={"content": {"text": "hello"}},
        actor=Actor(kind="agent", id="ag_demo"),
        created_at=datetime.now(UTC),
        trace_id=f"tr_{uuid.uuid4()}",
    )


class ForwardingRunner:
    """Fires `ctx.on_sub_agent_event_callback` N times, returns `{}`.

    Stands in for the real `AgentRunner` — the point of these tests is
    the observer channel plumbing, not the agent-invocation lifecycle.
    """

    def __init__(self, *, event_count: int = 1) -> None:
        self._event_count = event_count
        self.forwarded: list[Event] = []

    async def run(
        self,
        *,
        node_name: str,
        node_def: Any,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: Any,
    ) -> dict[str, Any]:
        _ = (node_name, node_def, bound_inputs, state)
        forward = ctx.on_sub_agent_event_callback
        for _ in range(self._event_count):
            evt = _make_agent_message_event(
                thread_id=f"th_{uuid.uuid4()}",
            )
            self.forwarded.append(evt)
            if forward is not None:
                forward(evt)
        return {}


def _make_workflow() -> Workflow:
    return Workflow.model_validate(
        {
            "nodes": {"only": {"type": "kolsch/nodes/noop"}},
            "graph": {"edges": []},
        }
    )


def _make_executor(
    tmp_path: Path,
    *,
    runner: ForwardingRunner,
    sub_agent_event_observer: Any = None,
) -> tuple[IterativeExecutor, WorkflowRunStore, Any]:
    """Build an executor with the given sub-agent observer."""
    store = WorkflowRunStore(tmp_path)
    wf = _make_workflow()
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/noop", runner)
    ex = IterativeExecutor(
        workflow=wf,
        run=run,
        store=store,
        registry=registry,
        catalog=AssetCatalog(),
        sub_agent_event_observer=sub_agent_event_observer,
    )
    return ex, store, run


# ---------------------------------------------------------------------------
# Test 1 — Observer receives `(run_id, node_name, invocation_id, event)`.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sub_agent_observer_receives_events(tmp_path: Path) -> None:
    """Every callback firing reaches the observer with correct id shape."""
    runner = ForwardingRunner(event_count=3)
    received: list[tuple[str, str, str, Event]] = []

    def observer(run_id: str, node_name: str, inv_id: str, event: Event) -> None:
        received.append((run_id, node_name, inv_id, event))

    ex, _store, run = _make_executor(
        tmp_path,
        runner=runner,
        sub_agent_event_observer=observer,
    )
    status = await ex.run()
    assert status == WorkflowRunStatus.SUCCEEDED
    assert len(received) == 3
    # spec-103 approach A: the emitting run's id is passed first — this
    # executor's own run.
    assert {r for r, _n, _i, _e in received} == {str(run.run_id)}
    # All three carry the same `node_name` + `invocation_id` (one
    # per-invocation callback binding — the executor's `_build_ctx`
    # captures both in the closure).
    assert {n for _r, n, _i, _e in received} == {"only"}
    inv_ids = {i for _r, _n, i, _e in received}
    assert len(inv_ids) == 1
    (only_inv,) = inv_ids
    assert only_inv.startswith("inv_")
    # Events are the same objects the runner forwarded, in order.
    assert [e for _r, _n, _i, e in received] == runner.forwarded


# ---------------------------------------------------------------------------
# Test 2 — No observer wired — callback is a no-op sink.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sub_agent_observer_none_runs_identically(tmp_path: Path) -> None:
    """A runner that fires the callback with no observer wired must
    complete without error and produce no observer side-effects."""
    runner = ForwardingRunner(event_count=5)
    ex, _store, _run = _make_executor(
        tmp_path,
        runner=runner,
        sub_agent_event_observer=None,
    )
    status = await ex.run()
    assert status == WorkflowRunStatus.SUCCEEDED
    assert len(runner.forwarded) == 5


# ---------------------------------------------------------------------------
# Test 3 — Raising observer is caught + logged; run completes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sub_agent_observer_exception_caught(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raising observer must NOT propagate into the executor."""
    runner = ForwardingRunner(event_count=2)

    def bad(run_id: str, node_name: str, inv_id: str, event: Event) -> None:
        _ = (run_id, node_name, inv_id, event)
        raise RuntimeError("bad sub-agent observer")

    ex, _store, _run = _make_executor(
        tmp_path,
        runner=runner,
        sub_agent_event_observer=bad,
    )
    with caplog.at_level(logging.ERROR, logger="workflows.executor"):
        status = await ex.run()
    assert status == WorkflowRunStatus.SUCCEEDED
    # `logger.exception` fired on each raise — two events, one raise
    # each.
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "sub_agent_event_observer raised" in joined
