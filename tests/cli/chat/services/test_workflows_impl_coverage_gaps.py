"""Coverage-gap tests for `WorkflowsImpl` error/branch paths.

Drives currently-uncovered branches of the chat-side workflow
service: layout-notify swallowing, cancel-flag write failure, orphan
sweep reconcile failure, orphan-notice degradation, dispatch observer
QueueFull / home-fallback branches, child-payload readers, bridge-drain
terminal transitions + node counters, cold-read meta/JSONL error paths,
the `_scan_tail_for_terminal` FAILED/SUCCEEDED/OSError branches, and the
`resolve_*` / `list_workflow_refs` catalog-miss/raise paths.

These are service-level unit tests that feed the observer + drain +
static-helper seams directly rather than standing up a real executor —
the happy-path integration coverage lives in the sibling
`test_subworkflow_lanes.py` / `test_workflows_impl_*` files.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from cli.chat.services.cards_impl import CardsImpl
from cli.chat.services.workflows_impl import (
    _STOP_SENTINEL,
    WorkflowRunRecord,
    WorkflowRunSpec,
    WorkflowsImpl,
    _scan_tail_for_terminal,
)
from schemas.events import EventType
from schemas.wire_kinds import WIRE_WORKFLOW_PROGRESS
from store import SessionStore, SqliteBackend
from tools.services.workflows import NodeCounts
from workflows.events import (
    WorkflowEvent,
    WorkflowNodeCalledWorkflowCompletedEvent,
    WorkflowNodeCalledWorkflowCompletedPayload,
    WorkflowNodeCalledWorkflowEvent,
    WorkflowNodeFailedEvent,
    WorkflowNodeTimedOutEvent,
    WorkflowRunCompletedEvent,
    WorkflowRunFailedEvent,
    WorkflowRunStartedEvent,
)
from workflows.ids import InvocationId, TraceId, WorkflowRunId
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures + builders.
# ---------------------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    """Yield a real SQLite-backed `SessionStore` rooted at ``tmp_path``."""
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
    *,
    cards: Any = None,
    runtime: Any = None,
) -> WorkflowsImpl:
    """Build a `WorkflowsImpl` bound to a fresh session/thread."""
    session = await store.create_session("ag_test", name="w")
    threads = await store.list_threads(session.session_id)
    run_store = WorkflowRunStore(tmp_path)
    return WorkflowsImpl(
        runtime=runtime,
        cards=cards,
        prompts=None,
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        run_store=run_store,
    )


def _record(
    run_id: WorkflowRunId,
    *,
    ref: str = "b/workflow/x",
    phase: str = "running",
    card: Any = None,
) -> WorkflowRunRecord:
    """Build a minimal live `WorkflowRunRecord`."""
    return WorkflowRunRecord(
        run_id=run_id,
        ref=ref,
        phase=phase,
        card=card,
        created_at=datetime.now(tz=UTC),
    )


def _spec(run_id: WorkflowRunId, *, card: Any = None) -> WorkflowRunSpec:
    """Build a `WorkflowRunSpec` for the drain helpers."""
    return WorkflowRunSpec(run_id=run_id, ref="b/workflow/x", inputs={}, card=card)


# ---------------------------------------------------------------------------
# _notify_layout — swallows a raising layout manager (437-438).
# ---------------------------------------------------------------------------


async def test_notify_layout_swallows_manager_raise(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A raising layout manager must not propagate out of `_notify_layout`."""
    impl = await _mk(tmp_path, store)
    try:

        class _BoomLayout:
            def notify_card_state_changed(self, run_id: WorkflowRunId) -> None:
                msg = "layout boom"
                raise RuntimeError(msg)

        impl.register_layout_manager(_BoomLayout())
        # Must not raise — the exception is logged and swallowed.
        impl._notify_layout(WorkflowRunId(f"wrk_{uuid4()}"))
    finally:
        await impl.aclose()


# ---------------------------------------------------------------------------
# cancel — OSError writing the cancel flag is logged + swallowed (611-612).
# ---------------------------------------------------------------------------


async def test_cancel_flag_write_oserror_is_swallowed(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A raising `cancel_flag_path.write_text` still cancels the run."""
    impl = await _mk(tmp_path, store)
    try:
        rid = WorkflowRunId(f"wrk_{uuid4()}")
        record = _record(rid, phase="running")

        class _BoomToken:
            def __init__(self) -> None:
                self.cancelled = False

            def cancel(self) -> None:
                self.cancelled = True

        class _BoomRunDir:
            @property
            def cancel_flag_path(self) -> Any:
                class _P:
                    def write_text(self, _text: str) -> None:
                        raise OSError("disk full")

                return _P()

        record.run_dir = _BoomRunDir()  # type: ignore[assignment]
        impl._workflow_registry[rid] = record
        token = _BoomToken()
        impl._workflow_cancels[rid] = token

        ok = await impl.cancel(rid)
        # Cancel still reports success (token fired, phase flipped) even
        # though the flag write raised.
        assert ok is True
        assert token.cancelled is True
        assert record.phase == "cancelling"
    finally:
        await impl.aclose()


# ---------------------------------------------------------------------------
# aclose — a per-run drain task that is already done skips cancel (698->697).
# ---------------------------------------------------------------------------


async def test_aclose_skips_already_done_drain_task(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """`aclose` does not re-cancel a per-run drain task that already finished."""
    impl = await _mk(tmp_path, store)

    async def _noop() -> None:
        return None

    loop = asyncio.get_running_loop()
    done_task: asyncio.Task[None] = loop.create_task(_noop())
    await done_task
    assert done_task.done()
    impl._per_run_drain_tasks[WorkflowRunId(f"wrk_{uuid4()}")] = done_task

    # aclose must complete cleanly, taking the `done()` skip branch.
    await impl.aclose()
    assert impl._closed is True


# ---------------------------------------------------------------------------
# sweep_orphans — a reconcile write failure logs + continues (775-781).
# ---------------------------------------------------------------------------


async def test_sweep_orphans_continues_on_reconcile_failure(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """One bad run's append failure must not derail the whole sweep."""
    run_store = WorkflowRunStore(tmp_path)
    good = run_store.create(
        workflow_ref="demo/workflows/good",
        inputs={},
        started_by="in-process",
    )
    run_store.update_status(good.run_id, WorkflowRunStatus.RUNNING)
    bad = run_store.create(
        workflow_ref="demo/workflows/bad",
        inputs={},
        started_by="in-process",
    )
    run_store.update_status(bad.run_id, WorkflowRunStatus.RUNNING)

    session = await store.create_session("ag_test", name="w")
    threads = await store.list_threads(session.session_id)
    impl = WorkflowsImpl(
        runtime=None,
        cards=None,
        prompts=None,
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        run_store=run_store,
    )

    real_append = run_store.append_event

    def _boom_append(run_id: Any, event: dict[str, Any]) -> Any:
        if str(run_id) == str(bad.run_id):
            raise OSError("append failed")
        return real_append(run_id, event)

    run_store.append_event = _boom_append  # type: ignore[method-assign]

    try:
        # Both runs are candidates; the bad one raises OSError inside the
        # try and is skipped, the good one reconciles.
        n = await impl.sweep_orphans()
        assert n == 1
    finally:
        await impl.aclose()


# ---------------------------------------------------------------------------
# fire_orphan_notice — cards=None / no notice attr / raising notice.
# (804, 807, 815-816)
# ---------------------------------------------------------------------------


async def test_fire_orphan_notice_no_cards_handle(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A `None` cards handle is silently skipped (804)."""
    impl = await _mk(tmp_path, store, cards=None)
    try:
        impl._pending_orphan_count = 3
        # cards is None → early return, counter still reset to 0.
        impl.fire_orphan_notice()
        assert impl._pending_orphan_count == 0
    finally:
        await impl.aclose()


async def test_fire_orphan_notice_cards_without_notice_attr(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A cards handle lacking a `.notice` attr is silently skipped (807)."""

    class _NoNoticeCards:
        pass

    impl = await _mk(tmp_path, store, cards=_NoNoticeCards())
    try:
        impl._pending_orphan_count = 1
        impl.fire_orphan_notice()  # getattr(cards, "notice", None) is None
        assert impl._pending_orphan_count == 0
    finally:
        await impl.aclose()


async def test_fire_orphan_notice_swallows_raising_notice(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A raising `Cards.notice` is logged + swallowed (815-816)."""

    class _BoomCards:
        def notice(self, text: str, *, kind: str = "info") -> None:
            _ = (text, kind)
            msg = "notice boom"
            raise RuntimeError(msg)

    impl = await _mk(tmp_path, store, cards=_BoomCards())
    try:
        impl._pending_orphan_count = 2
        # Must not raise despite the notice() blowing up.
        impl.fire_orphan_notice()
        assert impl._pending_orphan_count == 0
    finally:
        await impl.aclose()


# ---------------------------------------------------------------------------
# dispatch event observer — CALLED_WORKFLOW with no child_id (890->895);
# no lane + no home bridge → drop (905); QueueFull increments dropped (908-910).
# ---------------------------------------------------------------------------


async def test_dispatch_observer_called_workflow_without_child_id(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A `CALLED_WORKFLOW` whose payload lacks a child id skips the guard."""
    impl = await _mk(tmp_path, store)
    try:
        parent = WorkflowRunId(f"wrk_{uuid4()}")
        tr = TraceId(f"tr_{uuid4()}")
        record = _record(parent)
        impl._workflow_registry[parent] = record
        home_q: asyncio.Queue[Any] = asyncio.Queue()
        impl._workflow_bridges[parent] = home_q

        observer = impl._make_dispatch_event_observer(parent, record)
        # CALLED_WORKFLOW whose payload dict lacks `child_workflow_run_id`
        # → `_child_run_id_from_payload` returns None → the setdefault
        # race-guard branch is skipped (890->895). `model_construct`
        # bypasses the payload's `extra="forbid"` shape validation so we
        # can exercise the observer's defensive path. The event still
        # routes to the home bridge.
        called = WorkflowNodeCalledWorkflowEvent.model_construct(
            workflow_run_id=parent,
            trace_id=tr,
            invocation_id=InvocationId(f"inv_{uuid4()}"),
            node_name="sub",
            payload={},
        )
        observer(called)
        assert home_q.qsize() == 1
    finally:
        await impl.aclose()


async def test_dispatch_observer_drops_when_no_home_bridge(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """An unknown run id with no home bridge is silently dropped (905)."""
    impl = await _mk(tmp_path, store)
    try:
        home = WorkflowRunId(f"wrk_{uuid4()}")
        record = _record(home)
        # NB: no bridge registered for `home` at all.
        observer = impl._make_dispatch_event_observer(home, record)
        stray = WorkflowRunId(f"wrk_{uuid4()}")
        # Unknown lane + home bridge also missing → the second `queue is
        # None` guard returns without raising.
        observer(
            WorkflowRunStartedEvent(
                workflow_run_id=stray,
                trace_id=TraceId(f"tr_{uuid4()}"),
            )
        )
        assert home not in impl._workflow_bridges
    finally:
        await impl.aclose()


async def test_dispatch_observer_queuefull_increments_dropped(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A full bridge queue increments `record.dropped_events` (908-910)."""
    impl = await _mk(tmp_path, store)
    try:
        rid = WorkflowRunId(f"wrk_{uuid4()}")
        record = _record(rid)
        impl._workflow_registry[rid] = record
        full_q: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)
        full_q.put_nowait(object())  # saturate
        impl._workflow_bridges[rid] = full_q

        observer = impl._make_dispatch_event_observer(rid, record)
        observer(
            WorkflowRunStartedEvent(
                workflow_run_id=rid,
                trace_id=TraceId(f"tr_{uuid4()}"),
            )
        )
        assert record.dropped_events == 1
    finally:
        await impl.aclose()


# ---------------------------------------------------------------------------
# sub-agent dispatch observer — no home bridge → drop (946->949, 950);
# QueueFull increments dropped (959-961).
# ---------------------------------------------------------------------------


async def test_sub_agent_observer_drops_when_no_home_bridge(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """An unknown run id with no home bridge is dropped without raising."""
    impl = await _mk(tmp_path, store)
    try:
        home = WorkflowRunId(f"wrk_{uuid4()}")
        record = _record(home)
        observer = impl._make_dispatch_sub_agent_observer(home, record)
        from schemas.entities import Actor
        from schemas.events import Event
        from schemas.identifiers import EventId

        ev = Event(
            event_id=EventId(f"ev_{uuid4()}"),
            parent_event_id=None,
            thread_id=impl._thread_id,
            session_id=impl._session_id,
            type=EventType.AGENT_MESSAGE,
            data={},
            actor=Actor(kind="agent", id="ag_x"),
            created_at=datetime.now(UTC),
        )
        # Unknown key + missing home bridge → both `queue is None` guards
        # fire (946->949 then 950 return).
        observer(f"wrk_{uuid4()}", "node", "inv", ev)
        assert home not in impl._workflow_bridges
    finally:
        await impl.aclose()


async def test_sub_agent_observer_queuefull_increments_dropped(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A full bridge queue increments `dropped_events` (959-961)."""
    impl = await _mk(tmp_path, store)
    try:
        rid = WorkflowRunId(f"wrk_{uuid4()}")
        record = _record(rid)
        impl._workflow_registry[rid] = record
        full_q: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)
        full_q.put_nowait(object())
        impl._workflow_bridges[rid] = full_q

        observer = impl._make_dispatch_sub_agent_observer(rid, record)
        from schemas.entities import Actor
        from schemas.events import Event
        from schemas.identifiers import EventId

        ev = Event(
            event_id=EventId(f"ev_{uuid4()}"),
            parent_event_id=None,
            thread_id=impl._thread_id,
            session_id=impl._session_id,
            type=EventType.AGENT_MESSAGE,
            data={},
            actor=Actor(kind="agent", id="ag_x"),
            created_at=datetime.now(UTC),
        )
        observer(str(rid), "node", "inv", ev)
        assert record.dropped_events == 1
    finally:
        await impl.aclose()


# ---------------------------------------------------------------------------
# child-payload readers — dict fallback + None returns (976, 978, 986).
# ---------------------------------------------------------------------------


def test_child_run_id_from_payload_dict_and_none() -> None:
    """`_child_run_id_from_payload` reads dict shape and returns None on miss."""
    # dict path — attribute is absent, dict.get supplies the value (976).
    child_id = f"wrk_{uuid4()}"
    got = WorkflowsImpl._child_run_id_from_payload({"child_workflow_run_id": child_id})
    assert got is not None
    assert str(got) == child_id
    # None path — neither attribute nor dict key present (978).
    assert WorkflowsImpl._child_run_id_from_payload({}) is None
    assert WorkflowsImpl._child_run_id_from_payload(object()) is None


def test_child_ref_from_payload_dict_and_none() -> None:
    """`_child_ref_from_payload` reads the dict shape + None fallback (986)."""
    got = WorkflowsImpl._child_ref_from_payload({"child_workflow_ref": "b/w/c"})
    assert got == "b/w/c"
    assert WorkflowsImpl._child_ref_from_payload({}) is None


# ---------------------------------------------------------------------------
# _open_child_lane — a payload with no child id returns early (1007).
# ---------------------------------------------------------------------------


async def test_open_child_lane_no_child_id_returns_early(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """`_open_child_lane` returns without opening a card when child id absent."""
    impl = await _mk(tmp_path, store)
    try:
        parent = WorkflowRunId(f"wrk_{uuid4()}")
        before = len(impl._workflow_registry)
        await impl._open_child_lane(parent, {})  # child_id is None
        assert len(impl._workflow_registry) == before
    finally:
        await impl.aclose()


# ---------------------------------------------------------------------------
# _run_one_workflow — record missing (aclose race) → bail cleanly (1076).
# ---------------------------------------------------------------------------


async def test_run_one_workflow_missing_record_bails(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """`_run_one_workflow` returns immediately when the record is gone."""
    impl = await _mk(tmp_path, store)
    try:
        rid = WorkflowRunId(f"wrk_{uuid4()}")
        # No record registered → the `record is None` guard bails; no
        # cancel token / bridge should be created.
        await impl._run_one_workflow(_spec(rid))
        assert rid not in impl._workflow_cancels
        assert rid not in impl._workflow_bridges
    finally:
        await impl.aclose()


# ---------------------------------------------------------------------------
# _run_one_workflow — executor construction failure path (1162-1177 subset,
# 1174->1176). Factory raises → phase="error", error_message captured.
# ---------------------------------------------------------------------------


async def test_run_one_workflow_construction_failure_captures_error(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A raising executor factory flips the record to error with a message."""
    impl = await _mk(tmp_path, store)
    try:

        def _boom_factory(*_args: Any, **_kwargs: Any) -> Any:
            msg = "catalog blew up"
            raise RuntimeError(msg)

        impl._executor_factory = _boom_factory

        rid = await impl.start("kolsch/workflows/x", {})
        for _ in range(80):
            await asyncio.sleep(0.02)
            if impl.status(rid).phase == "error":
                break
        view = impl.status(rid)
        assert view.phase == "error"
        assert view.error_message is not None
        assert "catalog blew up" in view.error_message
    finally:
        await impl.aclose()


# ---------------------------------------------------------------------------
# _run_one_workflow — executor.run() raises after construction (1196-1222).
# Card closed with error + error_message stashed.
# ---------------------------------------------------------------------------


async def test_run_one_workflow_run_raise_closes_card_with_error(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A raising `executor.run()` closes the bound card with an error."""
    impl = await _mk(tmp_path, store)
    real_cards = CardsImpl(
        session_store=store,
        session_id=impl._session_id,
        thread_id=impl._thread_id,
    )
    try:
        from workflows.run import RunDirectory

        class _BoomExecutor:
            async def run(self) -> Any:
                msg = "run exploded"
                raise RuntimeError(msg)

        def _factory(
            ref: str,
            inputs: dict[str, Any],
            observer: Any,
            cancel_token: Any,
            sub_agent_observer: Any = None,
        ) -> tuple[Any, RunDirectory]:
            _ = (ref, inputs, observer, cancel_token, sub_agent_observer)
            run_dir = RunDirectory(root=tmp_path / "runs" / f"wrk_{uuid4()}")
            run_dir.root.mkdir(parents=True, exist_ok=True)
            return _BoomExecutor(), run_dir

        impl._executor_factory = _factory

        card = await real_cards.open("wf", kind="workflow")
        rid = await impl.start("kolsch/workflows/x", {}, card=card)
        for _ in range(80):
            await asyncio.sleep(0.02)
            if impl.status(rid).phase == "error":
                break
        view = impl.status(rid)
        assert view.phase == "error"
        assert view.error_message is not None
        assert "run exploded" in view.error_message
        assert card.is_closed
    finally:
        await impl.aclose()
        await real_cards.aclose()


# ---------------------------------------------------------------------------
# _drain_workflow_bridge — bridge queue absent → early return (1411).
# ---------------------------------------------------------------------------


async def test_drain_bridge_missing_queue_returns(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """The drain returns immediately when no bridge queue exists."""
    impl = await _mk(tmp_path, store)
    try:
        rid = WorkflowRunId(f"wrk_{uuid4()}")
        # No bridge queue → `_drain_workflow_bridge` returns at the guard.
        await impl._drain_workflow_bridge(_spec(rid), None)
        assert rid not in impl._workflow_bridges
    finally:
        await impl.aclose()


# ---------------------------------------------------------------------------
# bridge drain loop — record=None skips live-view updates (1494->1518);
# terminal RUN_FAILED with a card closes it; cancelling->cancelled preserved
# (1567->1574 .. 1587->1590, 1584).
# ---------------------------------------------------------------------------


async def _run_drain_loop(
    impl: WorkflowsImpl,
    spec: WorkflowRunSpec,
    events: list[WorkflowEvent | Any],
    *,
    record: WorkflowRunRecord | None,
) -> None:
    """Feed ``events`` (then a stop sentinel) through the inner drain loop."""
    q: asyncio.Queue[Any] = asyncio.Queue()
    impl._workflow_bridges[spec.run_id] = q
    for ev in events:
        q.put_nowait(ev)
    q.put_nowait(_STOP_SENTINEL)
    await impl._drain_workflow_bridge_loop(spec, q, record)


async def test_drain_loop_record_none_skips_live_view(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """With `record=None`, the drain still emits the UI event (1494->1518)."""
    impl = await _mk(tmp_path, store)
    try:
        rid = WorkflowRunId(f"wrk_{uuid4()}")
        tr = TraceId(f"tr_{uuid4()}")
        spec = _spec(rid)
        await _run_drain_loop(
            impl,
            spec,
            [WorkflowRunStartedEvent(workflow_run_id=rid, trace_id=tr)],
            record=None,
        )
        # A workflow.progress UI event landed even though record was None.
        found = False
        async for ev in store.read_events(impl._thread_id):
            if ev.type != EventType.UI_EVENT:
                continue
            data = ev.data if isinstance(ev.data, dict) else {}
            if data.get("kind") == WIRE_WORKFLOW_PROGRESS:
                found = True
                break
        assert found
    finally:
        await impl.aclose()


async def test_drain_loop_terminal_cancelling_preserved(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A `cancelling` record + RUN_ABORTED stays `cancelled` (1583-1584)."""
    from workflows.events import WorkflowRunAbortedEvent

    impl = await _mk(tmp_path, store)
    try:
        rid = WorkflowRunId(f"wrk_{uuid4()}")
        tr = TraceId(f"tr_{uuid4()}")
        record = _record(rid, phase="cancelling")
        impl._workflow_registry[rid] = record
        spec = _spec(rid)
        await _run_drain_loop(
            impl,
            spec,
            [WorkflowRunAbortedEvent(workflow_run_id=rid, trace_id=tr)],
            record=record,
        )
        assert record.phase == "cancelled"
        assert record.terminal_at is not None
    finally:
        await impl.aclose()


async def test_drain_loop_terminal_run_failed_closes_card(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A RUN_FAILED terminal closes the bound card with error (1590-1607)."""
    impl = await _mk(tmp_path, store)
    real_cards = CardsImpl(
        session_store=store,
        session_id=impl._session_id,
        thread_id=impl._thread_id,
    )
    try:
        rid = WorkflowRunId(f"wrk_{uuid4()}")
        tr = TraceId(f"tr_{uuid4()}")
        card = await real_cards.open("wf", kind="workflow")
        record = _record(rid, phase="running", card=card)
        record.error_message = "boom reason"
        impl._workflow_registry[rid] = record
        spec = _spec(rid, card=card)
        await _run_drain_loop(
            impl,
            spec,
            [
                WorkflowRunFailedEvent(
                    workflow_run_id=rid,
                    trace_id=tr,
                    payload={"reason": "boom reason"},
                ),
            ],
            record=record,
        )
        assert record.phase == "error"
        assert card.is_closed
    finally:
        await impl.aclose()
        await real_cards.aclose()


async def test_drain_loop_called_completed_posts_stop_sentinel(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """CALLED_WORKFLOW_COMPLETED posts a stop sentinel to the child bridge."""
    impl = await _mk(tmp_path, store)
    try:
        parent = WorkflowRunId(f"wrk_{uuid4()}")
        child = WorkflowRunId(f"wrk_{uuid4()}")
        tr = TraceId(f"tr_{uuid4()}")
        record = _record(parent)
        impl._workflow_registry[parent] = record
        # Register a child bridge so the completed event can post to it.
        child_q: asyncio.Queue[Any] = asyncio.Queue()
        impl._workflow_bridges[child] = child_q
        inv = InvocationId(f"inv_{uuid4()}")
        completed = WorkflowNodeCalledWorkflowCompletedEvent(
            workflow_run_id=parent,
            trace_id=tr,
            invocation_id=inv,
            node_name="sub",
            payload=WorkflowNodeCalledWorkflowCompletedPayload(
                node_name="sub",
                invocation_id=str(inv),
                child_workflow_run_id=str(child),
                child_status="succeeded",
            ),
        )
        await _run_drain_loop(impl, _spec(parent), [completed], record=record)
        # A stop sentinel was posted into the child's bridge queue.
        assert child_q.qsize() == 1
        assert isinstance(child_q.get_nowait(), type(_STOP_SENTINEL))
    finally:
        await impl.aclose()


# ---------------------------------------------------------------------------
# _maybe_capture_error — first-writer-wins early return (1640); node failure
# message + node_name (1646-1656); RUN_FAILED reason (1660).
# ---------------------------------------------------------------------------


def _rid() -> WorkflowRunId:
    return WorkflowRunId(f"wrk_{uuid4()}")


def test_maybe_capture_error_first_writer_wins() -> None:
    """`_maybe_capture_error` does not clobber an existing message (1640)."""
    rid = _rid()
    tr = TraceId(f"tr_{uuid4()}")
    record = _record(rid)
    record.error_message = "already set"
    ev = WorkflowNodeFailedEvent(
        workflow_run_id=rid,
        trace_id=tr,
        invocation_id=InvocationId(f"inv_{uuid4()}"),
        node_name="n",
        payload={"message": "would-overwrite"},
    )
    WorkflowsImpl._maybe_capture_error(record, ev)
    assert record.error_message == "already set"


def test_maybe_capture_error_node_failed_populates_fields() -> None:
    """A NODE_FAILED payload sets message + failed_node (1646-1656)."""
    rid = _rid()
    tr = TraceId(f"tr_{uuid4()}")
    record = _record(rid)
    ev = WorkflowNodeFailedEvent(
        workflow_run_id=rid,
        trace_id=tr,
        invocation_id=InvocationId(f"inv_{uuid4()}"),
        node_name="fetch",
        payload={"message": "boom in fetch", "node_name": "fetch"},
    )
    WorkflowsImpl._maybe_capture_error(record, ev)
    assert record.error_message == "boom in fetch"
    assert record.failed_node == "fetch"


def test_maybe_capture_error_node_timed_out_uses_envelope_node_name() -> None:
    """A NODE_TIMED_OUT without payload node_name falls back to envelope."""
    rid = _rid()
    tr = TraceId(f"tr_{uuid4()}")
    record = _record(rid)
    ev = WorkflowNodeTimedOutEvent(
        workflow_run_id=rid,
        trace_id=tr,
        invocation_id=InvocationId(f"inv_{uuid4()}"),
        node_name="slow_node",
        payload={"message": "timed out"},
    )
    WorkflowsImpl._maybe_capture_error(record, ev)
    assert record.error_message == "timed out"
    assert record.failed_node == "slow_node"


def test_maybe_capture_error_run_failed_reason() -> None:
    """A RUN_FAILED payload sets error_message from `reason` (1660)."""
    rid = _rid()
    tr = TraceId(f"tr_{uuid4()}")
    record = _record(rid)
    ev = WorkflowRunFailedEvent(
        workflow_run_id=rid,
        trace_id=tr,
        payload={"reason": "run failed overall"},
    )
    WorkflowsImpl._maybe_capture_error(record, ev)
    assert record.error_message == "run failed overall"
    assert record.failed_node is None


# ---------------------------------------------------------------------------
# _maybe_capture_outputs — first-writer-wins early return (1686).
# ---------------------------------------------------------------------------


def test_maybe_capture_outputs_first_writer_wins() -> None:
    """`_maybe_capture_outputs` does not overwrite existing outputs (1686)."""
    rid = _rid()
    tr = TraceId(f"tr_{uuid4()}")
    record = _record(rid)
    record.outputs = {"already": "here"}
    ev = WorkflowRunCompletedEvent(
        workflow_run_id=rid,
        trace_id=tr,
        payload={"outputs": {"new": "value"}},
    )
    WorkflowsImpl._maybe_capture_outputs(record, ev)
    assert record.outputs == {"already": "here"}


def test_maybe_capture_outputs_captures_on_run_completed() -> None:
    """A RUN_COMPLETED with a dict `outputs` populates the record."""
    rid = _rid()
    tr = TraceId(f"tr_{uuid4()}")
    record = _record(rid)
    ev = WorkflowRunCompletedEvent(
        workflow_run_id=rid,
        trace_id=tr,
        payload={"outputs": {"result": 42}},
    )
    WorkflowsImpl._maybe_capture_outputs(record, ev)
    assert record.outputs == {"result": 42}


# ---------------------------------------------------------------------------
# _update_node_counts — NODE_FAILED (1710-1711) + NODE_PRUNED (1716).
# ---------------------------------------------------------------------------


def test_update_node_counts_failed_and_pruned() -> None:
    """NODE_FAILED decrements running/increments failed; NODE_PRUNED completes."""
    from workflows.events import WorkflowEventKind

    counts = NodeCounts()
    WorkflowsImpl._update_node_counts(counts, WorkflowEventKind.NODE_STARTED)
    assert counts.running == 1
    # NODE_FAILED — running-1, failed+1 (1710-1711).
    WorkflowsImpl._update_node_counts(counts, WorkflowEventKind.NODE_FAILED)
    assert counts.running == 0
    assert counts.failed == 1
    # NODE_PRUNED — completed+1, no running change (1716).
    WorkflowsImpl._update_node_counts(counts, WorkflowEventKind.NODE_PRUNED)
    assert counts.completed == 1


# ---------------------------------------------------------------------------
# cold-read — meta.json unreadable/invalid → phase="unknown" (1748-1749);
# bad status enum → unknown (1754-1755); terminal_diag surfaced (1779).
# ---------------------------------------------------------------------------


def _mint_meta(
    runs_root: Path,
    run_id: WorkflowRunId,
    *,
    status: str,
    raw: str | None = None,
) -> Path:
    run_dir = runs_root / str(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    meta_path = run_dir / "meta.json"
    if raw is not None:
        meta_path.write_text(raw, encoding="utf-8")
        return meta_path
    meta = {
        "run_id": str(run_id),
        "trace_id": f"tr_{uuid4()}",
        "workflow_ref": "kolsch/workflows/test",
        "status": status,
        "inputs": {},
        "created_at": "2026-07-05T00:00:00+00:00",
    }
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    return meta_path


async def test_coldread_unreadable_meta_returns_unknown(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A meta.json with invalid JSON yields phase=unknown (1748-1749)."""
    impl = await _mk(tmp_path, store)
    try:
        rid = WorkflowRunId(f"wrk_{uuid4()}")
        _mint_meta(tmp_path / "runs", rid, status="running", raw="{not json")
        assert impl.status(rid).phase == "unknown"
    finally:
        await impl.aclose()


async def test_coldread_bad_status_enum_returns_unknown(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A meta.json with an unknown status string yields unknown (1754-1755)."""
    impl = await _mk(tmp_path, store)
    try:
        rid = WorkflowRunId(f"wrk_{uuid4()}")
        _mint_meta(tmp_path / "runs", rid, status="NOT_A_REAL_STATUS")
        assert impl.status(rid).phase == "unknown"
    finally:
        await impl.aclose()


async def test_coldread_failed_terminal_surfaces_diag(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A failed JSONL terminal surfaces error_message + failed_node (1779)."""
    impl = await _mk(tmp_path, store)
    try:
        rid = WorkflowRunId(f"wrk_{uuid4()}")
        _mint_meta(tmp_path / "runs", rid, status="running")
        events_path = tmp_path / "runs" / str(rid) / "events.jsonl"
        events_path.write_text(
            json.dumps(
                {
                    "event_id": f"ev_{uuid4()}",
                    "kind": "workflow.run.failed",
                    "workflow_run_id": str(rid),
                    "trace_id": f"tr_{uuid4()}",
                    "ts": "2026-07-05T00:00:01+00:00",
                    "payload": {"reason": "step X exploded", "node_name": "X"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        view = impl.status(rid)
        assert view.phase == "error"
        assert view.error_message == "step X exploded"
        assert view.failed_node == "X"
    finally:
        await impl.aclose()


# ---------------------------------------------------------------------------
# _scan_tail_for_terminal — direct unit coverage for the harder branches:
# stat OSError (2270-2271), read OSError (2277-2278), blank/undecodable lines
# skipped (2284, 2287-2288, 2290->2281), FAILED diag (2294-2304), SUCCEEDED
# outputs (2309->2315, 2311->2315, 2314), no-terminal fallthrough (2316).
# ---------------------------------------------------------------------------


class _StatBoomPath:
    """Path double whose `.stat()` raises OSError (2270-2271)."""

    def exists(self) -> bool:
        return True

    def stat(self) -> Any:
        raise OSError("stat failed")


class _ReadBoomPath:
    """Path double that exists + stats but whose `.open()` raises (2277-2278)."""

    def exists(self) -> bool:
        return True

    def stat(self) -> Any:
        class _S:
            st_size = 4096

        return _S()

    def open(self, _mode: str) -> Any:
        raise OSError("open failed")


def test_scan_tail_stat_oserror_returns_none() -> None:
    """A `.stat()` OSError yields the empty triple (2270-2271)."""
    assert _scan_tail_for_terminal(_StatBoomPath(), tail_bytes=8192) == (
        None,
        None,
        None,
    )


def test_scan_tail_read_oserror_returns_none() -> None:
    """An `.open()` OSError yields the empty triple (2277-2278)."""
    assert _scan_tail_for_terminal(_ReadBoomPath(), tail_bytes=8192) == (
        None,
        None,
        None,
    )


def test_scan_tail_skips_blank_and_bad_lines_then_finds_failed(
    tmp_path: Path,
) -> None:
    """Blank + non-JSON lines are skipped; a FAILED row wins (2284-2304)."""
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n"  # blank line skipped (2284)
        "{not json}\n"  # JSONDecodeError skipped (2287-2288)
        '{"kind": "workflow.node.started"}\n'  # non-terminal (2290->2281)
        + json.dumps(
            {
                "kind": "workflow.run.failed",
                "payload": {"reason": "explode", "node_name": "boom"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    status, diag, outputs = _scan_tail_for_terminal(path, tail_bytes=8192)
    assert status is WorkflowRunStatus.FAILED
    assert diag == ("explode", "boom")
    assert outputs is None


def test_scan_tail_succeeded_pulls_outputs(tmp_path: Path) -> None:
    """A SUCCEEDED row with dict outputs pulls them through (2309-2315)."""
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps(
            {
                "kind": "workflow.run.completed",
                "payload": {"outputs": {"answer": "yes"}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    status, diag, outputs = _scan_tail_for_terminal(path, tail_bytes=8192)
    assert status is WorkflowRunStatus.SUCCEEDED
    assert diag is None
    assert outputs == {"answer": "yes"}


def test_scan_tail_succeeded_non_dict_outputs(tmp_path: Path) -> None:
    """A SUCCEEDED row whose outputs is non-dict leaves outputs None (2311->2315)."""
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps(
            {
                "kind": "workflow.run.completed",
                "payload": {"outputs": "not-a-dict"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    status, _diag, outputs = _scan_tail_for_terminal(path, tail_bytes=8192)
    assert status is WorkflowRunStatus.SUCCEEDED
    assert outputs is None


def test_scan_tail_no_terminal_returns_none(tmp_path: Path) -> None:
    """A JSONL with only non-terminal rows falls through to None (2316)."""
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps({"kind": "workflow.node.completed"}) + "\n",
        encoding="utf-8",
    )
    assert _scan_tail_for_terminal(path, tail_bytes=8192) == (None, None, None)


# ---------------------------------------------------------------------------
# resolve_workflow_yaml / resolve_effective_required_inputs /
# validate_workflow_definition / list_workflow_refs — catalog miss + raise
# + OSError paths (1971, 1976-1977, 2022, 2026-2029, 2075, 2078-2084, 2104-2105).
# ---------------------------------------------------------------------------


class _FakeAssetMgmt:
    def __init__(self, catalog: Any) -> None:
        self._catalog = catalog

    def catalog(self) -> Any:
        if self._catalog is _BOOM:
            raise RuntimeError("catalog boom")
        return self._catalog


class _FakeRuntime:
    def __init__(self, catalog: Any) -> None:
        self.asset_management = _FakeAssetMgmt(catalog)


class _FakeCatalog:
    """Catalog double returning a preset entry from `.lookup`."""

    def __init__(self, entry: Any, *, assets: dict[str, Any] | None = None) -> None:
        self._entry = entry
        self.assets = assets or {}

    def lookup(self, ref: str, expected_type: str | None = None) -> Any:
        _ = (ref, expected_type)
        return self._entry


class _FakeEntry:
    def __init__(self, path: str, *, ref: str = "b/workflows/x") -> None:
        self.path = path
        self.ref = ref
        self.asset_type = "workflow"


_BOOM = object()


async def test_resolve_workflow_yaml_catalog_raise_returns_none(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A catalog that raises → resolve_workflow_yaml returns None."""
    impl = await _mk(tmp_path, store, runtime=_FakeRuntime(_BOOM))
    try:
        assert impl.resolve_workflow_yaml("b/workflows/x") is None
    finally:
        await impl.aclose()


async def test_resolve_workflow_yaml_missing_entry_returns_none(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A catalog with no matching entry → None (1971)."""
    impl = await _mk(tmp_path, store, runtime=_FakeRuntime(_FakeCatalog(None)))
    try:
        assert impl.resolve_workflow_yaml("b/workflows/x") is None
    finally:
        await impl.aclose()


async def test_resolve_workflow_yaml_read_oserror_returns_none(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """An entry whose path can't be read → None (1976-1977)."""
    missing = str(tmp_path / "does_not_exist.yaml")
    impl = await _mk(
        tmp_path,
        store,
        runtime=_FakeRuntime(_FakeCatalog(_FakeEntry(missing))),
    )
    try:
        assert impl.resolve_workflow_yaml("b/workflows/x") is None
    finally:
        await impl.aclose()


async def test_resolve_workflow_yaml_reads_source(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A readable entry path returns its YAML text (happy path)."""
    yaml_path = tmp_path / "wf.yaml"
    yaml_path.write_text("name: demo\n", encoding="utf-8")
    impl = await _mk(
        tmp_path,
        store,
        runtime=_FakeRuntime(_FakeCatalog(_FakeEntry(str(yaml_path)))),
    )
    try:
        got = impl.resolve_workflow_yaml("b/workflows/x")
        assert got == "name: demo\n"
    finally:
        await impl.aclose()


async def test_resolve_effective_required_inputs_catalog_raise(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A raising catalog → resolve_effective_required_inputs returns None."""
    impl = await _mk(tmp_path, store, runtime=_FakeRuntime(_BOOM))
    try:
        assert impl.resolve_effective_required_inputs("b/workflows/x") is None
    finally:
        await impl.aclose()


async def test_resolve_effective_required_inputs_missing_entry(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A missing entry → None (2022)."""
    impl = await _mk(tmp_path, store, runtime=_FakeRuntime(_FakeCatalog(None)))
    try:
        assert impl.resolve_effective_required_inputs("b/workflows/x") is None
    finally:
        await impl.aclose()


async def test_resolve_effective_required_inputs_read_oserror(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A path that can't be read → None (2026-2029)."""
    missing = str(tmp_path / "nope.yaml")
    impl = await _mk(
        tmp_path,
        store,
        runtime=_FakeRuntime(_FakeCatalog(_FakeEntry(missing))),
    )
    try:
        assert impl.resolve_effective_required_inputs("b/workflows/x") is None
    finally:
        await impl.aclose()


async def test_validate_workflow_definition_catalog_raise(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A raising catalog → validate_workflow_definition returns None."""
    impl = await _mk(tmp_path, store, runtime=_FakeRuntime(_BOOM))
    try:
        assert impl.validate_workflow_definition("b/workflows/x") is None
    finally:
        await impl.aclose()


async def test_validate_workflow_definition_missing_entry(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A missing entry → None (2075)."""
    impl = await _mk(tmp_path, store, runtime=_FakeRuntime(_FakeCatalog(None)))
    try:
        assert impl.validate_workflow_definition("b/workflows/x") is None
    finally:
        await impl.aclose()


async def test_validate_workflow_definition_missing_file_returns_diag(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A non-existent workflow file surfaces a diagnostic list (2078-2082).

    `load_workflow` wraps the underlying `OSError` in a
    `WorkflowValidationError`, so a missing file lands in the
    parse-error branch (returns ``[str(exc)]``) rather than the raw
    ``OSError`` fall-through.
    """
    missing = str(tmp_path / "gone.yaml")
    impl = await _mk(
        tmp_path,
        store,
        runtime=_FakeRuntime(_FakeCatalog(_FakeEntry(missing))),
    )
    try:
        result = impl.validate_workflow_definition("b/workflows/x")
        assert result is not None
        assert len(result) == 1
        assert isinstance(result[0], str)
    finally:
        await impl.aclose()


async def test_list_workflow_refs_catalog_raise_returns_empty(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A raising catalog → list_workflow_refs returns [] (2104-2105)."""
    impl = await _mk(tmp_path, store, runtime=_FakeRuntime(_BOOM))
    try:
        assert impl.list_workflow_refs() == []
    finally:
        await impl.aclose()


async def test_list_workflow_refs_returns_sorted_workflow_refs(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """`list_workflow_refs` returns only workflow-typed refs, sorted."""

    class _E:
        def __init__(self, ref: str, asset_type: str) -> None:
            self.ref = ref
            self.asset_type = asset_type

    assets = {
        "b/workflows/z": _E("b/workflows/z", "workflow"),
        "b/workflows/a": _E("b/workflows/a", "workflow"),
        "b/tools/t": _E("b/tools/t", "tool"),
    }
    catalog = _FakeCatalog(None, assets=assets)
    impl = await _mk(tmp_path, store, runtime=_FakeRuntime(catalog))
    try:
        assert impl.list_workflow_refs() == ["b/workflows/a", "b/workflows/z"]
    finally:
        await impl.aclose()


# ---------------------------------------------------------------------------
# _finalize_child_lane — no-terminal exit closes card as fallback + a
# NODE_COMPLETED bridge path exercises 1259->1286 finalizer normalisation via
# the child lane's own drain shutdown.
# ---------------------------------------------------------------------------


async def test_finalize_child_lane_fallback_closes_card(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A child lane that exits without a terminal closes its card as fallback."""
    impl = await _mk(tmp_path, store)
    real_cards = CardsImpl(
        session_store=store,
        session_id=impl._session_id,
        thread_id=impl._thread_id,
    )
    try:
        child = WorkflowRunId(f"wrk_{uuid4()}")
        card = await real_cards.open("child", kind="workflow")
        record = _record(child, phase="running", card=card)
        impl._workflow_registry[child] = record
        impl._workflow_bridges[child] = asyncio.Queue()
        impl._per_run_drain_tasks[child] = asyncio.get_running_loop().create_task(
            _noop_coro()
        )
        await impl._per_run_drain_tasks[child]

        # No terminal event ran → phase stays running; finalize closes the
        # card as a fallback + drops the trackers.
        await impl._finalize_child_lane(_spec(child, card=card), record)
        assert card.is_closed
        assert child not in impl._per_run_drain_tasks
        assert child not in impl._workflow_bridges
        assert record.terminal_at is not None
    finally:
        await impl.aclose()
        await real_cards.aclose()


async def _noop_coro() -> None:
    return None
