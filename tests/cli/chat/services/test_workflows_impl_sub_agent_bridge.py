"""spec-020 — `WorkflowsImpl` sub-agent bridge translation.

The executor's `sub_agent_event_observer` fans session-scoped
`Event`s into the per-run bridge queue wrapped in
`_SubAgentBridgeItem`. The bridge drain translates each into a
`kolsch.workflow.node.sub_agent_event` UI event with the spec-020
envelope (`run_id`, `node_name`, `invocation_id`, `trace_id`,
`event_type`, `event_id`, `payload`).

Three passes:

1. A synthetic sub-agent forwarder fires two events; two matching
   UI events land on the session store's JSONL with the correct
   envelope.
2. `model.request` events are dropped as noise (v1 filter — spec-020
   §Open questions); `model.response` still forwards.
3. The forwarder terminates naturally when the workflow node
   reaches its terminal state — no sub-agent UI events land AFTER
   the workflow's terminal `RUN_COMPLETED` UI event.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cli.chat.services.cards_impl import CardsImpl
from cli.chat.services.workflows_impl import (
    WorkflowsImpl,
    _SubAgentBridgeItem,
)
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId
from schemas.wire_kinds import (
    WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT,
    WIRE_WORKFLOW_PROGRESS,
)
from store import SessionStore, SqliteBackend
from workflows.events import (
    WorkflowEvent,
    WorkflowNodeStartedEvent,
    WorkflowRunCompletedEvent,
    WorkflowRunStartedEvent,
)
from workflows.ids import InvocationId, TraceId, WorkflowRunId
from workflows.run_store import WorkflowRunStore

# ---------------------------------------------------------------------------
# Fixtures + helpers.
# ---------------------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    try:
        yield SessionStore(root=tmp_path, backend=backend)
    finally:
        await backend.close()


async def _mk(
    tmp_path: Path,
    store: SessionStore,
) -> tuple[WorkflowsImpl, CardsImpl]:
    session = await store.create_session("ag_test", name="w")
    threads = await store.list_threads(session.session_id)
    run_store = WorkflowRunStore(tmp_path)
    cards = CardsImpl(
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
    )
    impl = WorkflowsImpl(
        runtime=None,
        cards=cards,
        prompts=None,
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        run_store=run_store,
    )
    return impl, cards


def _make_event(
    *,
    event_type: EventType,
    trace_id: str,
    data: dict[str, Any] | None = None,
) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid.uuid4()}"),
        parent_event_id=None,
        thread_id=ThreadId(f"th_{uuid.uuid4()}"),
        session_id=SessionId(f"ses_{uuid.uuid4()}"),
        type=event_type,
        data=dict(data or {}),
        actor=Actor(kind="agent", id="ag_x"),
        created_at=datetime.now(UTC),
        trace_id=trace_id,
    )


async def _collect(
    store: SessionStore,
    thread_id: str,
    kind: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    async for ev in store.read_events(thread_id):
        if ev.type != EventType.UI_EVENT:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        if data.get("kind") == kind:
            payload = data.get("payload") or {}
            if isinstance(payload, dict):
                out.append(payload)
    return out


async def _wait_terminal(impl: WorkflowsImpl, rid: WorkflowRunId) -> None:
    for _ in range(80):
        await asyncio.sleep(0.05)
        phase = impl.status(rid).phase
        if phase in ("success", "error", "cancelled"):
            return


def _install_sub_agent_mock(
    impl: WorkflowsImpl,
    *,
    ref: str,
    tmp_path: Path,
    events: list[WorkflowEvent],
    sub_agent_events: list[tuple[str, str, Event]],
    per_event_delay: float = 0.0,
) -> None:
    """Mock executor factory that also fires the sub-agent observer.

    Emits the given `WorkflowEvent`s in order, then dispatches the
    scripted `(node_name, invocation_id, event)` triples to the
    executor's `sub_agent_event_observer` before RUN_COMPLETED.
    """
    from workflows.run import RunDirectory

    class _MockWithSubAgent:
        def __init__(
            self,
            *,
            events: list[WorkflowEvent],
            observer: Callable[[WorkflowEvent], None],
            cancel_token: Any,
            sub_agent_observer: Any,
            per_event_delay: float,
        ) -> None:
            self._events = list(events)
            self._observer = observer
            self._cancel_token = cancel_token
            self._sub_agent_observer = sub_agent_observer
            self._per_event_delay = per_event_delay

        async def run(self) -> Any:
            from workflows.events import WorkflowEventKind
            from workflows.run import WorkflowRunStatus

            for idx, event in enumerate(self._events):
                self._observer(event)
                if self._per_event_delay > 0:
                    await asyncio.sleep(self._per_event_delay)
                # After NODE_STARTED, fan the sub-agent events.
                if (
                    event.kind == WorkflowEventKind.NODE_STARTED
                    and self._sub_agent_observer is not None
                    and sub_agent_events
                ):
                    # spec-103 approach A: the executor passes the
                    # emitting run's id first. Use the event's own
                    # `workflow_run_id`; the service's run-dispatching
                    # observer falls back to the home (this run's) bridge
                    # for an unrecognised id, so the item still routes to
                    # this run's card.
                    emitting_run_id = str(event.workflow_run_id)
                    for nn, inv, evt in sub_agent_events:
                        self._sub_agent_observer(emitting_run_id, nn, inv, evt)
                        if self._per_event_delay > 0:
                            await asyncio.sleep(self._per_event_delay)
            # Terminal status derivation.
            if self._events:
                last = self._events[-1]
                if last.kind == WorkflowEventKind.RUN_COMPLETED:
                    return WorkflowRunStatus.SUCCEEDED
                if last.kind == WorkflowEventKind.RUN_FAILED:
                    return WorkflowRunStatus.FAILED
                if last.kind == WorkflowEventKind.RUN_TIMED_OUT:
                    return WorkflowRunStatus.TIMED_OUT
                if last.kind == WorkflowEventKind.RUN_ABORTED:
                    return WorkflowRunStatus.ABORTED
            return WorkflowRunStatus.SUCCEEDED

    def _factory(
        ref_in: str,
        inputs: dict[str, Any],
        observer: Callable[[WorkflowEvent], None],
        cancel_token: Any,
        sub_agent_observer: Any = None,
    ) -> tuple[Any, RunDirectory]:
        _ = (ref_in, inputs)
        run_dir = RunDirectory(
            root=tmp_path / "runs" / f"wrk_{uuid.uuid4()}",
        )
        run_dir.root.mkdir(parents=True, exist_ok=True)
        return (
            _MockWithSubAgent(
                events=events,
                observer=observer,
                cancel_token=cancel_token,
                sub_agent_observer=sub_agent_observer,
                per_event_delay=per_event_delay,
            ),
            run_dir,
        )

    impl._executor_factory = _factory


# ---------------------------------------------------------------------------
# Test 1 — Envelope shape (happy path).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_emits_sub_agent_events_with_envelope(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """Sub-agent events land on the JSONL as UI events with the
    spec-020 envelope, one per fired forwarder event."""
    impl, cards = await _mk(tmp_path, store)
    try:
        rid_seed = WorkflowRunId(f"wrk_{uuid4()}")
        tr = TraceId(f"tr_{uuid4()}")
        inv = InvocationId(f"inv_{uuid4()}")
        events = [
            WorkflowRunStartedEvent(workflow_run_id=rid_seed, trace_id=tr),
            WorkflowNodeStartedEvent(
                workflow_run_id=rid_seed,
                trace_id=tr,
                invocation_id=inv,
                node_name="classify",
            ),
            WorkflowRunCompletedEvent(workflow_run_id=rid_seed, trace_id=tr),
        ]
        # Two sub-agent events — a tool.called followed by an
        # agent.message. Both should land as UI events.
        sub_events = [
            (
                "classify",
                str(inv),
                _make_event(
                    event_type=EventType.TOOL_CALLED,
                    trace_id=str(tr),
                    data={"tool_name": "bash", "args": {"cmd": "ls"}},
                ),
            ),
            (
                "classify",
                str(inv),
                _make_event(
                    event_type=EventType.AGENT_MESSAGE,
                    trace_id=str(tr),
                    data={"content": {"label": "positive"}},
                ),
            ),
        ]
        _install_sub_agent_mock(
            impl,
            ref="kolsch/workflows/x",
            tmp_path=tmp_path,
            events=events,
            sub_agent_events=sub_events,
        )
        card = await cards.open("wf", kind="workflow")
        rid = await impl.start("kolsch/workflows/x", {}, card=card)
        await _wait_terminal(impl, rid)
        await asyncio.sleep(0.15)

        emitted = await _collect(
            store,
            cards._thread_id,
            WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT,
        )
        assert len(emitted) == 2, f"expected 2 sub-agent UI events, got {len(emitted)}"
        # Envelope shape — every key spec-020 lists.
        for env, (_nn, _inv, src_event) in zip(emitted, sub_events, strict=True):
            assert env["run_id"] == str(rid)
            assert env["node_name"] == "classify"
            assert env["invocation_id"] == str(inv)
            assert env["trace_id"] == str(tr)
            assert env["event_type"] == str(src_event.type.value)
            assert env["event_id"] == str(src_event.event_id)
            assert env["payload"] == dict(src_event.data)
    finally:
        await impl.aclose()
        await cards.aclose()


# ---------------------------------------------------------------------------
# Test 2 — Noise filter.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_drops_model_request_events(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """spec-020 §Open questions — `model.request` is redundant with
    the follow-up `model.response`; the bridge drops it."""
    impl, cards = await _mk(tmp_path, store)
    try:
        rid_seed = WorkflowRunId(f"wrk_{uuid4()}")
        tr = TraceId(f"tr_{uuid4()}")
        inv = InvocationId(f"inv_{uuid4()}")
        events = [
            WorkflowRunStartedEvent(workflow_run_id=rid_seed, trace_id=tr),
            WorkflowNodeStartedEvent(
                workflow_run_id=rid_seed,
                trace_id=tr,
                invocation_id=inv,
                node_name="classify",
            ),
            WorkflowRunCompletedEvent(workflow_run_id=rid_seed, trace_id=tr),
        ]
        sub_events = [
            (
                "classify",
                str(inv),
                _make_event(
                    event_type=EventType.MODEL_REQUEST,
                    trace_id=str(tr),
                ),
            ),
            (
                "classify",
                str(inv),
                _make_event(
                    event_type=EventType.MODEL_RESPONSE,
                    trace_id=str(tr),
                ),
            ),
        ]
        _install_sub_agent_mock(
            impl,
            ref="kolsch/workflows/x",
            tmp_path=tmp_path,
            events=events,
            sub_agent_events=sub_events,
        )
        card = await cards.open("wf", kind="workflow")
        rid = await impl.start("kolsch/workflows/x", {}, card=card)
        await _wait_terminal(impl, rid)
        await asyncio.sleep(0.15)

        emitted = await _collect(
            store,
            cards._thread_id,
            WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT,
        )
        assert len(emitted) == 1, emitted
        assert emitted[0]["event_type"] == "model.response"
    finally:
        await impl.aclose()
        await cards.aclose()


# ---------------------------------------------------------------------------
# Test 3 — Forwarder lifecycle: no events after node terminal.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_terminates_after_workflow_terminal(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """No sub-agent UI events land AFTER the workflow's terminal
    `RUN_COMPLETED` progress event — the observer stops naturally
    when the runner returns."""
    impl, cards = await _mk(tmp_path, store)
    try:
        rid_seed = WorkflowRunId(f"wrk_{uuid4()}")
        tr = TraceId(f"tr_{uuid4()}")
        inv = InvocationId(f"inv_{uuid4()}")
        events = [
            WorkflowRunStartedEvent(workflow_run_id=rid_seed, trace_id=tr),
            WorkflowNodeStartedEvent(
                workflow_run_id=rid_seed,
                trace_id=tr,
                invocation_id=inv,
                node_name="classify",
            ),
            WorkflowRunCompletedEvent(workflow_run_id=rid_seed, trace_id=tr),
        ]
        sub_events = [
            (
                "classify",
                str(inv),
                _make_event(
                    event_type=EventType.AGENT_MESSAGE,
                    trace_id=str(tr),
                ),
            ),
        ]
        _install_sub_agent_mock(
            impl,
            ref="kolsch/workflows/x",
            tmp_path=tmp_path,
            events=events,
            sub_agent_events=sub_events,
        )
        card = await cards.open("wf", kind="workflow")
        rid = await impl.start("kolsch/workflows/x", {}, card=card)
        await _wait_terminal(impl, rid)
        await asyncio.sleep(0.3)  # generous tail

        # Read all UI events in order; assert every sub-agent UI event
        # appears BEFORE the terminal `RUN_COMPLETED` progress event.
        kinds_in_order: list[str] = []
        wire_kinds: list[tuple[str, str]] = []  # (wire_kind, extra)
        async for ev in store.read_events(cards._thread_id):
            if ev.type != EventType.UI_EVENT:
                continue
            data = ev.data if isinstance(ev.data, dict) else {}
            wk = data.get("kind")
            if not isinstance(wk, str):
                continue
            payload = data.get("payload") or {}
            if wk == WIRE_WORKFLOW_PROGRESS and isinstance(payload, dict):
                wire_kinds.append((wk, str(payload.get("kind") or "")))
            else:
                wire_kinds.append((wk, ""))
            kinds_in_order.append(wk)

        # Terminal progress event is present.
        terminal_indices = [
            i
            for i, (wk, inner) in enumerate(wire_kinds)
            if wk == WIRE_WORKFLOW_PROGRESS and inner == "workflow.run.completed"
        ]
        assert terminal_indices, "no RUN_COMPLETED progress event landed"
        last_terminal = terminal_indices[-1]
        # Every sub-agent UI event appears at or before `last_terminal`.
        sub_agent_indices = [
            i
            for i, (wk, _inner) in enumerate(wire_kinds)
            if wk == WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT
        ]
        assert sub_agent_indices, "sub-agent events were dropped entirely"
        assert max(sub_agent_indices) < last_terminal, (
            "sub-agent event landed AFTER the terminal progress event — "
            "observer wasn't torn down at node terminal"
        )
    finally:
        await impl.aclose()
        await cards.aclose()


# ---------------------------------------------------------------------------
# Test 4 — _SubAgentBridgeItem is a documented public shape.
# ---------------------------------------------------------------------------


def test_sub_agent_bridge_item_is_frozen_dataclass() -> None:
    """The bridge envelope is `frozen=True` — no in-flight mutation."""
    item = _SubAgentBridgeItem(
        node_name="classify",
        invocation_id="inv_x",
        event=_make_event(
            event_type=EventType.AGENT_MESSAGE,
            trace_id="tr_x",
        ),
    )
    with pytest.raises((AttributeError, Exception)):
        item.node_name = "other"  # type: ignore[misc]
