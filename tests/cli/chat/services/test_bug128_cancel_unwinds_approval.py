"""bug-128 — `cancel()` unwinds a node parked on an in-node approval.

The executor only observes the cancel token at node boundaries / around
LLM streams, so a node blocked on a bare ``await prompts.choice(...)``
future never sees it. `cancel()` now additionally drives
``WIRE_PROMPT_CANCELLED`` for the run's pending prompt (adapter →
``PromptsImpl.cancel_pending`` → ``CancelledError`` → the run unwinds).
The resulting stray ``CancelledError`` propagates through the shared
FIFO-drain task — which must SURVIVE (the task itself wasn't cancelled)
so subsequent runs keep processing.

Coverage:

1. The shared drain survives a stray ``CancelledError`` from one run and
   keeps serving the next (the ``.cancelling()`` guard in
   ``_run_one_workflow``).
2. ``cancel()`` appends exactly one ``WIRE_PROMPT_CANCELLED`` for the
   run's pending prompt when the injected lookup matches.
3. ``cancel()`` emits NO ``WIRE_PROMPT_CANCELLED`` when the lookup does
   not match (and the token/flag path is unchanged) — no cross-run
   prompt cancellation.

The consume side (``WIRE_PROMPT_CANCELLED`` → ``cancel_pending`` +
``⊘ Interrupted`` widget flip) is the shipped bug-110 path, covered by
``tests/cli/chat/test_bug110_live_cancel_unfreezes_composer.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from cli.chat.services.workflows_impl import WorkflowsImpl
from schemas.events import EventType
from schemas.wire_kinds import WIRE_PROMPT_CANCELLED
from store import SessionStore, SqliteBackend
from tests.cli.chat.services.mocks import (
    MockExecutorSpec,
    install_mock_executor_factory,
)
from workflows.events import (
    WorkflowEvent,
    WorkflowRunCompletedEvent,
    WorkflowRunStartedEvent,
)
from workflows.ids import TraceId, WorkflowRunId
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


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


async def _mk_impl(tmp_path: Path, store: SessionStore) -> WorkflowsImpl:
    session = await store.create_session("ag_test", name="w")
    threads = await store.list_threads(session.session_id)
    return WorkflowsImpl(
        runtime=None,
        cards=None,
        prompts=None,
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        run_store=WorkflowRunStore(tmp_path),
    )


def _completing_events() -> list[WorkflowEvent]:
    rid = WorkflowRunId(f"wrk_{uuid4()}")
    tr = TraceId(f"tr_{uuid4()}")
    return [
        WorkflowRunStartedEvent(workflow_run_id=rid, trace_id=tr),
        WorkflowRunCompletedEvent(workflow_run_id=rid, trace_id=tr),
    ]


def _started_only() -> list[WorkflowEvent]:
    rid = WorkflowRunId(f"wrk_{uuid4()}")
    tr = TraceId(f"tr_{uuid4()}")
    return [WorkflowRunStartedEvent(workflow_run_id=rid, trace_id=tr)]


async def _wait_terminal(impl: WorkflowsImpl, rid: WorkflowRunId) -> str:
    for _ in range(100):
        await asyncio.sleep(0.03)
        phase = impl.status(rid).phase
        if phase in ("success", "error", "cancelled"):
            return phase
    return impl.status(rid).phase


async def _wait_running(impl: WorkflowsImpl, rid: WorkflowRunId) -> None:
    for _ in range(60):
        await asyncio.sleep(0.03)
        rec = impl._workflow_registry.get(rid)
        if rec is not None and rec.run_dir is not None and rec.phase == "running":
            return


async def test_drain_survives_stray_cancel(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A stray CancelledError from one run must not kill the FIFO drain."""
    impl = await _mk_impl(tmp_path, store)
    try:
        install_mock_executor_factory(
            impl,
            {
                # First run raises a stray CancelledError mid-flight.
                "kolsch/workflows/boom": MockExecutorSpec(
                    events=_started_only(),
                    raise_cancelled=True,
                ),
                # Second run completes normally — proves the drain lived.
                "kolsch/workflows/ok": MockExecutorSpec(
                    events=_completing_events(),
                ),
            },
            runs_root=tmp_path / "runs",
        )
        boom = await impl.start("kolsch/workflows/boom", {})
        ok = await impl.start("kolsch/workflows/ok", {})

        ok_phase = await _wait_terminal(impl, ok)
        boom_phase = await _wait_terminal(impl, boom)

        # The drain survived the stray cancel and served the next run.
        assert ok_phase == "success", f"drain died — ok never ran ({ok_phase})"
        # The cancelled run reached a terminal phase (normalised).
        assert boom_phase in ("error", "cancelled")
    finally:
        await impl.aclose()


async def test_cancel_emits_prompt_cancelled_for_matched_run(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """cancel() drives WIRE_PROMPT_CANCELLED for the run's pending prompt."""
    impl = await _mk_impl(tmp_path, store)
    try:
        install_mock_executor_factory(
            impl,
            {
                "kolsch/workflows/slow": MockExecutorSpec(
                    events=_completing_events(),
                    per_event_delay=0.5,
                ),
            },
            runs_root=tmp_path / "runs",
        )
        rid = await impl.start("kolsch/workflows/slow", {})
        await _wait_running(impl, rid)

        # bug-066: the prompt's `source.run_id` is the STORE-minted
        # on-disk id (`run_dir.root.name`), NOT the queue-time registry
        # key `rid`. `cancel()` must look the prompt up by the on-disk id
        # — matching on `rid` here would silently never fire (the
        # production bug the first review caught). Assert the two id
        # spaces actually differ so this test can't pass by luck.
        on_disk_id = impl._workflow_registry[rid].run_dir.root.name
        assert str(on_disk_id) != str(rid)
        impl.set_pending_prompt_lookup(
            lambda run_id: "prm_bug128" if str(run_id) == str(on_disk_id) else None
        )

        ok = await impl.cancel(rid)  # rid = queue-time id
        assert ok is True

        payloads: list[dict[str, Any]] = []
        async for ev in store.read_events(impl._thread_id):
            if ev.type != EventType.UI_EVENT or not isinstance(ev.data, dict):
                continue
            if ev.data.get("kind") == WIRE_PROMPT_CANCELLED:
                payloads.append(ev.data.get("payload") or {})
        assert len(payloads) == 1, f"expected exactly one cancel, got {payloads}"
        assert payloads[0].get("prompt_id") == "prm_bug128"
        assert payloads[0].get("reason") == "workflow_cancelled"
    finally:
        await impl.aclose()


async def test_cancel_no_prompt_cancel_when_unmatched(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """No pending prompt for the run → no WIRE_PROMPT_CANCELLED; token still fires."""
    impl = await _mk_impl(tmp_path, store)
    try:
        install_mock_executor_factory(
            impl,
            {
                "kolsch/workflows/slow": MockExecutorSpec(
                    events=_completing_events(),
                    per_event_delay=0.5,
                ),
            },
            runs_root=tmp_path / "runs",
        )
        rid = await impl.start("kolsch/workflows/slow", {})
        await _wait_running(impl, rid)
        token = impl._workflow_cancels[rid]

        # Lookup never matches (a prompt for some OTHER run, or none).
        impl.set_pending_prompt_lookup(lambda _run_id: None)

        ok = await impl.cancel(rid)
        assert ok is True
        assert token.is_cancelled()  # normal token/flag path unchanged

        cancels = [
            1
            async for ev in store.read_events(impl._thread_id)
            if ev.type == EventType.UI_EVENT
            and isinstance(ev.data, dict)
            and ev.data.get("kind") == WIRE_PROMPT_CANCELLED
        ]
        assert not cancels, "must not cancel a prompt that isn't this run's"
    finally:
        await impl.aclose()


async def test_cancel_does_not_clobber_terminal_under_inline_bus_dispatch(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """bug-128 Finding A: a finalize during the emit must not be clobbered.

    The WIRE_PROMPT_CANCELLED emit publishes on the bus, which dispatches
    subscribers INLINE (within the `await publish`). In production that
    dispatch wakes the parked node, and its unwind runs `_finalize_terminal`
    (flipping the record terminal) on the drain task mid-emit. `cancel()` must
    write `phase` only BEFORE that yield and never after — else it clobbers the
    terminal `cancelled` back to `cancelling` (a permanent phantom).
    """
    from bus.in_process import InProcessEventBus
    from cli.chat.services.cards_impl import UI_EVENT_TOPIC

    bus = InProcessEventBus()
    session = await store.create_session("ag_test", name="w")
    threads = await store.list_threads(session.session_id)
    impl = WorkflowsImpl(
        runtime=None,
        cards=None,
        prompts=None,
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        run_store=WorkflowRunStore(tmp_path),
        bus=bus,
    )
    try:
        install_mock_executor_factory(
            impl,
            {
                "kolsch/workflows/slow": MockExecutorSpec(
                    events=_completing_events(),
                    per_event_delay=0.5,
                ),
            },
            runs_root=tmp_path / "runs",
        )
        rid = await impl.start("kolsch/workflows/slow", {})
        await _wait_running(impl, rid)
        on_disk_id = impl._workflow_registry[rid].run_dir.root.name
        impl.set_pending_prompt_lookup(
            lambda run_id: "prm_race" if str(run_id) == str(on_disk_id) else None
        )

        # Simulate the adapter's inline dispatch racing the drain: on the
        # WIRE_PROMPT_CANCELLED event, finalize the record + yield (as
        # `_finalize_terminal` + `render_terminal` do during `cancel_pending`).
        async def racing_subscriber(event: object) -> None:
            data = getattr(event, "data", None)
            if isinstance(data, dict) and data.get("kind") == WIRE_PROMPT_CANCELLED:
                rec = impl._workflow_registry.get(rid)
                if rec is not None:
                    rec.phase = "cancelled"
                await asyncio.sleep(0)

        await bus.subscribe(UI_EVENT_TOPIC, racing_subscriber)

        await impl.cancel(rid)

        # Asserted immediately — the real drain is still mid `sleep(0.5)`, so
        # the only writer of `phase` after the subscriber is `cancel()` itself.
        assert impl._workflow_registry[rid].phase == "cancelled", (
            "cancel() clobbered a terminal phase back to cancelling"
        )
    finally:
        await impl.aclose()
        await bus.close()


async def test_cancel_looks_up_prompt_by_on_disk_id(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """bug-066 regression: cancel() must query the lookup with the on-disk id.

    The prompt's `source.run_id` is the store-minted `run_dir.root.name`,
    not the queue-time registry key. Passing the wrong id makes the whole
    fix a silent no-op (the first review's blocking finding).
    """
    impl = await _mk_impl(tmp_path, store)
    try:
        install_mock_executor_factory(
            impl,
            {
                "kolsch/workflows/slow": MockExecutorSpec(
                    events=_completing_events(),
                    per_event_delay=0.5,
                ),
            },
            runs_root=tmp_path / "runs",
        )
        rid = await impl.start("kolsch/workflows/slow", {})
        await _wait_running(impl, rid)
        on_disk_id = impl._workflow_registry[rid].run_dir.root.name

        seen: list[str] = []

        def _spy(run_id: object) -> None:
            seen.append(str(run_id))

        impl.set_pending_prompt_lookup(_spy)
        await impl.cancel(rid)

        assert str(on_disk_id) in seen, "cancel must query by the on-disk id"
        assert str(rid) not in seen, "cancel must NOT query by the queue-time id"
    finally:
        await impl.aclose()


async def test_finalize_reconciles_meta_with_on_disk_id(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """bug-066 regression (AC5): meta reconciliation targets the on-disk id.

    A run that unwinds without a terminal JSONL event (the CancelledError
    path) reconciles `meta.json` so a cold-read isn't a phantom `running`.
    `meta.json` is keyed by the store id — reconciling the queue-time id
    silently FileNotFoundErrors (the second review's blocking finding).
    """
    impl = await _mk_impl(tmp_path, store)
    calls: list[str] = []

    def _spy(run_id: object, _status: object) -> None:
        # Record only the id — the mock mints no meta.json, so calling
        # through would just FileNotFoundError. The id is what we verify.
        calls.append(str(run_id))

    impl._run_store.update_status = _spy  # type: ignore[assignment]
    try:
        install_mock_executor_factory(
            impl,
            {
                "kolsch/workflows/boom": MockExecutorSpec(
                    events=_started_only(),
                    raise_cancelled=True,
                ),
            },
            runs_root=tmp_path / "runs",
        )
        rid = await impl.start("kolsch/workflows/boom", {})
        await _wait_terminal(impl, rid)
        rec = impl._workflow_registry.get(rid)
        assert rec is not None and rec.run_dir is not None
        on_disk_id = rec.run_dir.root.name

        assert str(on_disk_id) in calls, "reconcile must target the on-disk id"
        assert str(rid) not in calls, "reconcile must NOT target the queue-time id"
    finally:
        await impl.aclose()


def test_real_closure_matches_by_on_disk_id() -> None:
    """The production on_mount closure resolves the prompt via the on-disk id.

    Wires the REAL `EventAdapter.run_id_for_prompt` (over a populated
    `_workflow_prompt_sources`, keyed by the store-minted `source.run_id`)
    into the same closure `ChatApp.on_mount` builds, and confirms it
    returns the pending prompt_id only for the on-disk id.
    """
    from cli.chat.event_adapter import EventAdapter

    adapter = EventAdapter(SimpleNamespace())
    store_id = f"wrk_{uuid4()}"
    queue_id = f"wrk_{uuid4()}"
    prompt_id = "prm_real"
    # Populated on the live workflow-scoped prompt-requested branch,
    # keyed by the STORE id (source.run_id), not the queue-time id.
    adapter._workflow_prompt_sources[prompt_id] = (store_id, "node_a")

    prompts = SimpleNamespace(pending_prompt_id=prompt_id)

    # Replica of the ChatApp.on_mount closure.
    def lookup(run_id: object) -> str | None:
        pid = prompts.pending_prompt_id
        if pid is None:
            return None
        owner = adapter.run_id_for_prompt(str(pid))
        return str(pid) if owner == str(run_id) else None

    assert lookup(store_id) == prompt_id  # on-disk id matches
    assert lookup(queue_id) is None  # queue-time id must NOT match
    assert adapter.run_id_for_prompt("prm_unknown") is None
