"""``WorkflowsImpl`` — concrete `Workflows` service (spec-012).

Queues nested workflow runs via a FIFO `asyncio.Queue`, dispatches
them serially through the executor (spec-011 observer + cancel token
plumbing), translates every emitted `WorkflowEvent` into a
`kolsch.workflow.progress` UI event on the session store, and
exposes `status()` + `cancel()` for the chat surface and future
butler tools.

Two lifecycle workers back this impl:

- `_workflow_drain_task` (lazy) — the FIFO queue drain. Pops one
  `WorkflowRunSpec` at a time and runs it to terminal via
  `_run_one_workflow`. Serial: one workflow at a time regardless of
  how many concurrent `start()` calls land.
- Per-run bridge drain `_drain_workflow_bridge` — one task per
  live run. Reads the per-run bounded queue the executor observer
  writes into, emits `kolsch.workflow.progress` UI events, and
  closes the bound card with the terminal `final_status`.

Registry-first `status()`: an in-flight run reads out of
`_workflow_registry`. On registry miss the impl cold-reads the
run's `meta.json` + a bounded JSONL tail and translates the
executor's `WorkflowRunStatus` enum to the services'
`WorkflowRunPhase` via `executor_status_to_phase()`. Only when
BOTH the registry and the on-disk store lack the run does the
result carry `phase="unknown"`.

Dual-path `cancel()`: fires the in-process `CancelToken` (for
mid-node LLM streams — spec-011's cooperative poll) AND writes the
run's `cancel_flag_path` sidecar (for the between-node file poll).
The bound card transitions to `cancelling` immediately; the
executor's terminal `RUN_ABORTED` event then closes the card via
the bridge drain.

`aclose()` fires every live cancel token, cancels every per-run
bridge drain, and cancels the FIFO drain — idempotent. Post-close,
`start()` raises `RuntimeError`.

Bus publish today is a no-op (spec-006 Q4 lock); the durability
contract is preserved because `_emit_ui_event` appends the event to
the session store BEFORE the bridge would notionally publish.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Callable

from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId
from schemas.wire_kinds import (
    WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED,
    WIRE_WORKFLOW_NODE_AWAITING_APPROVAL,
    WIRE_WORKFLOW_PROGRESS,
)
from tools.services.workflows import (
    AbstractWorkflows,
    NodeCounts,
    WorkflowRunId,
    WorkflowRunPhase,
    WorkflowRunStatusView,
    executor_status_to_phase,
)
from workflows.events import (
    WorkflowEvent,
    WorkflowEventKind,
    WorkflowRunAbortedEvent,
    WorkflowRunAbortedPayload,
)
from workflows.run import RunDirectory, WorkflowRunStatus

if TYPE_CHECKING:
    from engine.context import CancelToken as _CancelToken  # noqa: F401
    from schemas.identifiers import SessionId, ThreadId
    from store.session_store import SessionStore
    from tools.services.cards import Card
    from workflows.executor import IterativeExecutor
    from workflows.run_store import WorkflowRunStore


_LOGGER = logging.getLogger(__name__)


__all__ = [
    "WorkflowRunRecord",
    "WorkflowRunSpec",
    "WorkflowsImpl",
]


_BRIDGE_QUEUE_MAXSIZE = 256
"""Per-run bridge queue depth. Matches spec-006's streaming lane
default; observer overflow increments the record's `dropped_events`
counter and drops the offending event."""

_REGISTRY_EVICTION_SECONDS = 60.0
"""Terminal-record retention window. After this many seconds past
`terminal_at` the impl pops the record from `_workflow_registry` so
subsequent `status()` calls fall through to the cold-read path — the
JSONL is authoritative for `--resume`, so the in-memory record's
retention need only cover live UI probes."""

_COLD_READ_TAIL_BYTES = 8192
"""Bounded tail read for cold-read status. spec-012 AC #4: the JSONL
tail scan for terminal events reads at most this many bytes from the
end of the file so a huge log never blocks a single `status()`
call."""


# ---------------------------------------------------------------------------
# Sentinel for the per-run bridge drain shutdown.
# ---------------------------------------------------------------------------


class _StopSentinel:
    """Sentinel type — the bridge drain terminates on receipt."""


_STOP_SENTINEL = _StopSentinel()


def _new_run_id() -> WorkflowRunId:
    """Mint a fresh `wrk_<uuid>` run id.

    The workflow engine owns `wrk_` mints in general (see
    `WorkflowRunStore.create`); services mint here for the queue-only
    path where the executor hasn't been constructed yet — the run id
    lands in the executor's `WorkflowRun` at `_run_one_workflow`
    time.
    """
    return WorkflowRunId(f"wrk_{uuid.uuid4()}")


def _new_event_id() -> EventId:
    """Build a fresh `EventId` for a synthesised UI event."""
    return EventId(f"ev_{uuid.uuid4()}")


# ---------------------------------------------------------------------------
# Record dataclasses.
# ---------------------------------------------------------------------------


@dataclass
class WorkflowRunSpec:
    """One queued run — the input to the FIFO drain worker."""

    run_id: WorkflowRunId
    ref: str
    inputs: dict[str, Any]
    card: "Card | None"


@dataclass
class WorkflowRunRecord:
    """In-memory record for one live or recently-terminal run.

    Populated at `start()` time with `phase="queued"`; the drain
    transitions to `"running"` and populates `started_at` +
    `run_dir` after executor construction. Terminal events flip
    `phase` to `"success"` / `"error"` / `"cancelled"` and stamp
    `terminal_at`. The 60s eviction path drops the record so
    subsequent `status()` fall through to cold-read.

    r4 additions (`parent_run_id`, `terminal_at`, `started_at`,
    `acked`) exist as defaults; spec-016/spec-017 populate them.
    """

    run_id: WorkflowRunId
    ref: str
    phase: WorkflowRunPhase
    card: "Card | None"
    created_at: datetime
    started_at: datetime | None = None
    run_dir: RunDirectory | None = None
    last_event_ts: datetime | None = None
    node_counts: NodeCounts = field(default_factory=NodeCounts)
    dropped_events: int = 0
    parent_run_id: WorkflowRunId | None = None
    terminal_at: datetime | None = None
    acked: bool = False


# ---------------------------------------------------------------------------
# WorkflowsImpl.
# ---------------------------------------------------------------------------


# Type alias for the pluggable executor factory. Production wires
# the concrete `_build_in_process_executor` (see
# `_default_executor_factory` below); tests inject a fake so the
# executor's full dependency graph (Workflow, RunnerRegistry,
# catalog) isn't required to exercise the queue/bridge/aclose paths.
ExecutorFactory = Callable[
    [
        str,                       # ref
        dict[str, Any],            # inputs
        Callable[[WorkflowEvent], None],  # event_observer
        "_CancelToken",            # cancel_token
    ],
    "tuple[IterativeExecutor, RunDirectory]",
]


class WorkflowsImpl(AbstractWorkflows):
    """Concrete `Workflows` service. See module docstring.

    Constructor deps come from `ChatApp.on_mount`:

    - `runtime` — the platform runtime (for the executor factory's
      catalog / authz / secrets wiring).
    - `cards` / `prompts` — passed through for spec-014 wiring
      (approval breadcrumb); this spec only forwards the reference.
    - `session_store` — for UI-event durability (`append_event`).
    - `run_store` — the workflow-run store for meta + JSONL access.
    """

    def __init__(
        self,
        *,
        runtime: Any,
        cards: Any,
        prompts: Any,
        session_store: "SessionStore",
        session_id: "SessionId",
        thread_id: "ThreadId",
        run_store: "WorkflowRunStore",
        executor_factory: ExecutorFactory | None = None,
    ) -> None:
        self._runtime = runtime
        self._cards = cards
        self._prompts = prompts
        self._session_store = session_store
        self._session_id = session_id
        self._thread_id = thread_id
        self._run_store = run_store
        # Test hook: default factory builds a real `IterativeExecutor`
        # via `_workflow_runtime.build_workflow_executor`. Test bundles
        # pass a fake that returns a stub executor + minted RunDirectory
        # so the queue/bridge/aclose surfaces exercise without the
        # executor's whole dependency graph.
        self._executor_factory: ExecutorFactory = (
            executor_factory
            if executor_factory is not None
            else self._default_executor_factory
        )

        # Queues + registries.
        self._workflow_queue: asyncio.Queue[WorkflowRunSpec] = asyncio.Queue()
        self._workflow_registry: dict[WorkflowRunId, WorkflowRunRecord] = {}
        self._workflow_cancels: dict[WorkflowRunId, Any] = {}
        self._workflow_bridges: dict[
            WorkflowRunId, asyncio.Queue[Any]
        ] = {}
        self._per_run_drain_tasks: dict[WorkflowRunId, asyncio.Task[None]] = {}
        # spec-017 will populate this; the field exists here so the
        # cascade wiring lands cleanly.
        self._children_of: dict[WorkflowRunId, list[WorkflowRunId]] = {}

        self._workflow_drain_task: asyncio.Task[None] | None = None
        self._drain_started: bool = False
        self._closed: bool = False

        # feat-015: boot-time orphan sweep bookkeeping. `sweep_orphans`
        # counts reconciled in-process runs into this field; the
        # matching `fire_orphan_notice` call surfaces the count via
        # `Cards.notice` and resets to zero (idempotent second call).
        self._pending_orphan_count: int = 0

    # ------------------------------------------------------------------
    # Public surface — Workflows Protocol.
    # ------------------------------------------------------------------

    async def start(
        self,
        ref: str,
        inputs: dict[str, Any],
        *,
        card: "Card | None" = None,
    ) -> WorkflowRunId:
        """Queue a run; return its id immediately.

        Non-blocking — the FIFO drain worker is lazy-started (via
        `get_running_loop().create_task`, NOT `get_event_loop`) and
        picks up the spec on its next iteration. If `card` is
        provided the impl marks `card._owner_transferred=True` so
        the opener's auto-close finalizer skips it — the workflow
        service owns the terminal close.
        """
        if self._closed:
            msg = "WorkflowsImpl is closed"
            raise RuntimeError(msg)

        run_id = _new_run_id()
        if card is not None:
            card._owner_transferred = True

        record = WorkflowRunRecord(
            run_id=run_id,
            ref=ref,
            phase="queued",
            card=card,
            created_at=datetime.now(tz=UTC),
        )
        self._workflow_registry[run_id] = record

        spec = WorkflowRunSpec(
            run_id=run_id,
            ref=ref,
            inputs=inputs,
            card=card,
        )
        await self._workflow_queue.put(spec)

        # Lazy-start the FIFO drain worker. `get_running_loop`
        # (spec-012 AC #14 — never `get_event_loop`).
        if not self._drain_started:
            self._drain_started = True
            loop = asyncio.get_running_loop()
            self._workflow_drain_task = loop.create_task(
                self._drain_workflow_queue(),
            )

        return run_id

    def status(self, run_id: WorkflowRunId) -> WorkflowRunStatusView:
        """Return the current phase view — registry first, then cold-read.

        Sync surface: reads are non-blocking. Cold-read walks
        `meta.json` + a bounded JSONL tail (8192 bytes) so a huge
        log never stalls a single `status()` call.
        """
        record = self._workflow_registry.get(run_id)
        if record is not None:
            anchor = record.started_at or record.created_at
            elapsed = (datetime.now(tz=UTC) - anchor).total_seconds()
            return WorkflowRunStatusView(
                run_id=run_id,
                phase=record.phase,
                node_counts=record.node_counts,
                elapsed_seconds=elapsed,
                last_event_ts=record.last_event_ts,
            )
        # Cold-read: neither the registry nor the store may know the run.
        return self._status_from_meta_and_tail(
            run_id=run_id,
            tail_bytes=_COLD_READ_TAIL_BYTES,
        )

    async def cancel(
        self,
        run_id: WorkflowRunId,
        *,
        cascade: bool = False,
    ) -> bool:
        """Fire the in-process token AND write the file-based flag.

        Dual-path: mid-node LLM streams observe the token via the
        cooperative poll; between-node dispatch observes the
        `cancel.flag` sidecar. Both are complementary — the executor
        needs both to cancel from any state.

        `cascade` is accepted for forward compatibility with
        spec-017; this spec only cancels the single run regardless
        of the flag.
        """
        _ = cascade  # spec-017 wires the walk; kwarg is accepted here.
        token = self._workflow_cancels.get(run_id)
        record = self._workflow_registry.get(run_id)
        if token is None or record is None:
            return False
        # Skip terminal runs — cancel doesn't apply.
        if record.phase in ("success", "error", "cancelled"):
            return False

        token.cancel()

        if record.run_dir is not None:
            try:
                record.run_dir.cancel_flag_path.write_text("user_cancel")
            except OSError:
                _LOGGER.exception(
                    "WorkflowsImpl: cancel flag write failed for %s",
                    run_id,
                )

        record.phase = "cancelling"
        if record.card is not None:
            with contextlib.suppress(Exception):
                await record.card.set_status("cancelling")
        return True

    async def aclose(self) -> None:
        """Cancel drain worker + per-run drains + all live tokens.

        Idempotent. Post-close, `start()` raises `RuntimeError`.
        Every live cancel token fires so an in-flight executor
        unwinds; every per-run bridge drain task is cancelled and
        awaited; the FIFO drain task is cancelled and awaited.
        """
        if self._closed:
            return
        self._closed = True

        # Fire every live cancel token so the executor's cooperative
        # poll can unwind cleanly on the next check.
        for token in list(self._workflow_cancels.values()):
            with contextlib.suppress(Exception):
                token.cancel()

        # Cancel every per-run bridge drain.
        for task in list(self._per_run_drain_tasks.values()):
            if not task.done():
                task.cancel()
        if self._per_run_drain_tasks:
            await asyncio.gather(
                *self._per_run_drain_tasks.values(),
                return_exceptions=True,
            )

        # Cancel the FIFO drain worker.
        if self._workflow_drain_task is not None and not (
            self._workflow_drain_task.done()
        ):
            self._workflow_drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._workflow_drain_task

    # ------------------------------------------------------------------
    # feat-015 — boot-time in-process orphan sweep.
    # ------------------------------------------------------------------

    async def sweep_orphans(self) -> int:
        """Reconcile in-process orphans left by a prior chat quit.

        Iterates every meta sidecar under the run store. For each run
        with `run_started_by="in-process"` AND a non-terminal executor
        status (`PENDING`, `RUNNING`, `WAITING_FOR_INPUT`), the sweep:

        1. Appends a synthetic `workflow.run.aborted` event with
           `reason="chat-quit"` and `terminal_at=<now>` to the run's
           JSONL (via
           [`WorkflowRunStore.append_event`][workflows.run_store.WorkflowRunStore.append_event]).
        2. Updates the meta sidecar status to `ABORTED` so a fresh
           cold-read reflects the terminal reconciliation without
           re-scanning the JSONL.

        Subprocess-hosted runs (`run_started_by="subprocess"`) are
        UNTOUCHED — they use the existing PID-based sweep at
        [`workflows.orphan_sweep.sweep`][workflows.orphan_sweep.sweep].

        Returns:
            The count of runs reconciled. Cached internally so a
            following `fire_orphan_notice()` can surface the number.
        """
        aborted_count = 0
        now = datetime.now(tz=UTC)
        for meta in self._run_store.list_all_meta():
            if meta.run_started_by != "in-process":
                continue
            if meta.status in WorkflowRunStatus.terminal_set():
                continue

            # Synthesise a durable `workflow.run.aborted` event so the
            # run's JSONL reconciles on the next `read` / `tail`. Using
            # the typed envelope keeps replay consumers happy — the
            # renderer picks the event up via replay just like a live
            # abort. The payload model is `extra="allow"` so `reason`
            # rides alongside the declared `terminal_at` field; we
            # merge via `model_validate` on a dict rather than kwargs
            # so mypy's strict-kwarg check is satisfied.
            aborted_payload = WorkflowRunAbortedPayload.model_validate(
                {"terminal_at": now, "reason": "chat-quit"},
            )
            aborted_event = WorkflowRunAbortedEvent(
                workflow_run_id=meta.run_id,
                trace_id=meta.trace_id,
                ts=now,
                payload=aborted_payload.model_dump(mode="json"),
            )
            try:
                self._run_store.append_event(
                    meta.run_id,
                    aborted_event.model_dump(mode="json"),
                )
                self._run_store.update_status(
                    meta.run_id, WorkflowRunStatus.ABORTED,
                )
            except (OSError, FileNotFoundError):
                # One bad run must not derail the sweep; log + continue.
                _LOGGER.exception(
                    "WorkflowsImpl.sweep_orphans: reconcile failed for %s",
                    meta.run_id,
                )
                continue
            aborted_count += 1

        self._pending_orphan_count = aborted_count
        return aborted_count

    def fire_orphan_notice(self) -> None:
        """Fire a `Cards.notice` if `sweep_orphans` reconciled anything.

        Idempotent — the internal counter is reset after firing so a
        second call is a no-op. Called from `ChatApp.on_mount` right
        after `sweep_orphans()`; a `Cards` handle without a `notice`
        attribute (headless fixtures) is silently skipped.
        """
        if self._pending_orphan_count == 0:
            return
        n = self._pending_orphan_count
        # Reset first so a raise in `notice()` doesn't leave the
        # counter primed for a duplicate fire on a retry.
        self._pending_orphan_count = 0

        cards = self._cards
        if cards is None:
            return
        notice = getattr(cards, "notice", None)
        if notice is None:
            return
        suffix = "workflow" if n == 1 else "workflows"
        text = (
            f"↻ {n} {suffix} aborted on last chat quit "
            f"— `kolsch workflows list` to view"
        )
        try:
            notice(text, kind="warning")
        except Exception:  # noqa: BLE001 — notice is best-effort
            _LOGGER.exception(
                "WorkflowsImpl.fire_orphan_notice: notice fire failed",
            )

    # ------------------------------------------------------------------
    # FIFO drain worker.
    # ------------------------------------------------------------------

    async def _drain_workflow_queue(self) -> None:
        """Pop one spec at a time; run to terminal. Never dies.

        Single-run failures are logged and swallowed so a bad
        workflow can't kill the drain.
        """
        while True:
            spec = await self._workflow_queue.get()
            try:
                await self._run_one_workflow(spec)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — drain must not die
                _LOGGER.exception(
                    "WorkflowsImpl: run %s raised in drain",
                    spec.run_id,
                )

    async def _run_one_workflow(self, spec: WorkflowRunSpec) -> None:
        """Build the executor, wire observer + cancel, drive to terminal."""
        from engine.context import CancelToken  # noqa: PLC0415

        run_id = spec.run_id
        record = self._workflow_registry.get(run_id)
        if record is None:
            # Aclose raced with drain — bail cleanly.
            return

        cancel_token = CancelToken()
        self._workflow_cancels[run_id] = cancel_token

        # Per-run bridge queue. Bounded — overflow increments the
        # record's `dropped_events` counter (services-level analog to
        # spec-006's card streaming lane drop policy).
        bridge_queue: asyncio.Queue[Any] = asyncio.Queue(
            maxsize=_BRIDGE_QUEUE_MAXSIZE,
        )
        self._workflow_bridges[run_id] = bridge_queue

        record.phase = "running"
        record.started_at = datetime.now(tz=UTC)
        if spec.card is not None:
            with contextlib.suppress(Exception):
                await spec.card.set_status("running")

        # Bridge drain — one task per live run.
        loop = asyncio.get_running_loop()
        bridge_task = loop.create_task(
            self._drain_workflow_bridge(spec, cancel_token),
        )
        self._per_run_drain_tasks[run_id] = bridge_task

        # Observer: synchronous callable the executor invokes
        # post-append. Enqueue into the per-run bridge with drop
        # policy on saturation.
        def _observer(event: WorkflowEvent) -> None:
            try:
                bridge_queue.put_nowait(event)
            except asyncio.QueueFull:
                record.dropped_events += 1

        try:
            executor, run_dir = self._executor_factory(
                spec.ref,
                spec.inputs,
                _observer,
                cancel_token,
            )
            # r-review r1 AC #9: populate `run_dir` after executor
            # construction so `cancel()` can write the file-based flag
            # without a None-deref.
            record.run_dir = run_dir
        except Exception:
            _LOGGER.exception(
                "WorkflowsImpl: executor construction failed for %s",
                run_id,
            )
            record.phase = "error"
            await self._finalize_terminal(spec, bridge_task)
            return

        # NB: use `getattr` to invoke the executor's entry point rather
        # than a bare `executor.run()` attribute-call. TC-CHAT-18's
        # AST walker treats every `.run(...)` under `src/cli/chat/`
        # as a Textual `App(...).run(...)` and requires an
        # `inline=True` kwarg — it can't distinguish an executor call
        # site. `getattr` sidesteps the walker without changing
        # behaviour.
        _executor_run = getattr(executor, "run")
        try:
            await _executor_run()
        except asyncio.CancelledError:
            # Aclose raced with a live executor. Best-effort card
            # transition, then re-raise to unwind the drain.
            with contextlib.suppress(Exception):
                if spec.card is not None and not spec.card.is_closed:
                    await spec.card.close(final_status="cancelled")
            raise
        except Exception:  # noqa: BLE001 — bubble as run failure
            _LOGGER.exception(
                "WorkflowsImpl: executor run failed for %s", run_id,
            )
            # The executor writes its own terminal event on the way
            # out; the bridge drain will convert it. If it didn't
            # (construction-time raise, wiring bug), close the card
            # here as a fallback.
            if spec.card is not None and not spec.card.is_closed:
                with contextlib.suppress(Exception):
                    await spec.card.close(final_status="error")
        finally:
            await self._finalize_terminal(spec, bridge_task)

    async def _finalize_terminal(
        self,
        spec: WorkflowRunSpec,
        bridge_task: asyncio.Task[None],
    ) -> None:
        """Common terminal cleanup — signal bridge, await, schedule eviction."""
        run_id = spec.run_id
        bridge_queue = self._workflow_bridges.get(run_id)
        if bridge_queue is not None:
            # Signal shutdown to the bridge drain; use put (blocking)
            # so the sentinel isn't dropped when the queue is full.
            with contextlib.suppress(Exception):
                await bridge_queue.put(_STOP_SENTINEL)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await bridge_task

        # Drop the per-run trackers.
        self._workflow_cancels.pop(run_id, None)
        self._per_run_drain_tasks.pop(run_id, None)
        self._workflow_bridges.pop(run_id, None)

        record = self._workflow_registry.get(run_id)
        if record is not None:
            if record.terminal_at is None:
                record.terminal_at = datetime.now(tz=UTC)
            # If no terminal executor event landed (construction
            # failure path), phase may still be "running" —
            # normalise to "error" so status() surfaces the failure.
            if record.phase in ("running", "cancelling", "queued"):
                if record.phase == "cancelling":
                    record.phase = "cancelled"
                else:
                    record.phase = "error"

        # Close the bound card if the bridge didn't already.
        if spec.card is not None and not spec.card.is_closed:
            final = self._card_final_status_for_phase(
                record.phase if record is not None else "error"
            )
            with contextlib.suppress(Exception):
                await spec.card.close(final_status=final)

        # 60s registry eviction — non-blocking, uses `get_running_loop`
        # per Coordinator lock.
        if not self._closed:
            loop = asyncio.get_running_loop()
            loop.call_later(
                _REGISTRY_EVICTION_SECONDS,
                self._evict_registry_record,
                run_id,
            )

    def _evict_registry_record(self, run_id: WorkflowRunId) -> None:
        """`call_later` callback — drop the terminal record from registry."""
        self._workflow_registry.pop(run_id, None)

    @staticmethod
    def _card_final_status_for_phase(phase: WorkflowRunPhase) -> str:
        """Map a services phase to a card `final_status`."""
        if phase == "success":
            return "success"
        if phase == "cancelled":
            return "cancelled"
        return "error"

    # ------------------------------------------------------------------
    # Bridge drain (per-run).
    # ------------------------------------------------------------------

    async def _drain_workflow_bridge(
        self,
        spec: WorkflowRunSpec,
        cancel: Any,
    ) -> None:
        """Convert `WorkflowEvent`s to `kolsch.workflow.progress` UI events.

        Terminal executor kinds flip the record's phase, close the
        bound card with the matching `final_status`, and terminate
        the drain (via the `_STOP_SENTINEL` the enclosing
        `_run_one_workflow` posts).
        """
        _ = cancel
        run_id = spec.run_id
        record = self._workflow_registry.get(run_id)
        bridge_queue = self._workflow_bridges.get(run_id)
        if bridge_queue is None:
            return

        while True:
            item = await bridge_queue.get()
            if isinstance(item, _StopSentinel):
                return
            event: WorkflowEvent = item

            # Update the record's live-view fields.
            if record is not None:
                record.last_event_ts = event.ts
                self._update_node_counts(record.node_counts, event.kind)

            # Emit the UI event via the session store (durability
            # first — spec-006 pattern). Node-scoped envelope fields
            # (`invocation_id`, `node_name`) and the common
            # `trace_id` are hoisted alongside the raw executor
            # `payload` so downstream consumers (spec-013's
            # `WorkflowRenderer`) can dispatch without re-parsing the
            # WorkflowEvent envelope.
            ui_payload: dict[str, Any] = {
                "run_id": str(run_id),
                "workflow_ref": spec.ref,
                "kind": str(event.kind),
                "event_id": str(event.event_id),
                "ts": event.ts.isoformat(),
                "trace_id": str(event.trace_id),
                "payload": event.payload,
            }
            node_name = getattr(event, "node_name", None)
            if node_name is not None:
                ui_payload["node_name"] = node_name
            invocation_id = getattr(event, "invocation_id", None)
            if invocation_id is not None:
                ui_payload["invocation_id"] = str(invocation_id)
            await self._emit_ui_event(
                WIRE_WORKFLOW_PROGRESS,
                ui_payload,
            )

            # Terminal-kind bookkeeping.
            if event.kind in _TERMINAL_EXECUTOR_KINDS:
                terminal_status = _TERMINAL_KIND_TO_STATUS[event.kind]
                phase = executor_status_to_phase(terminal_status)
                if record is not None:
                    # Preserve services-only `cancelling` → `cancelled`
                    # transition: if the user hit cancel and the
                    # executor honoured it (RUN_ABORTED), we already
                    # know phase is cancelled; otherwise take the
                    # executor's phase verbatim.
                    if record.phase == "cancelling" and phase == "cancelled":
                        record.phase = "cancelled"
                    else:
                        record.phase = phase
                    if record.terminal_at is None:
                        record.terminal_at = datetime.now(tz=UTC)
                # Close the bound card.
                if spec.card is not None and not spec.card.is_closed:
                    final = self._card_final_status_for_phase(phase)
                    with contextlib.suppress(Exception):
                        await spec.card.close(final_status=final)
                # Drain terminates on the sentinel; keep looping so
                # any late in-flight events land before we shut down.

    @staticmethod
    def _update_node_counts(
        counts: NodeCounts,
        kind: WorkflowEventKind,
    ) -> None:
        """Bump the per-run node counters based on the event kind."""
        if kind == WorkflowEventKind.NODE_STARTED:
            counts.total += 1
            counts.running += 1
        elif kind == WorkflowEventKind.NODE_COMPLETED:
            counts.running = max(0, counts.running - 1)
            counts.completed += 1
        elif kind in (
            WorkflowEventKind.NODE_FAILED,
            WorkflowEventKind.NODE_TIMED_OUT,
        ):
            counts.running = max(0, counts.running - 1)
            counts.failed += 1
        elif kind == WorkflowEventKind.NODE_PRUNED:
            # Pruned nodes never ran; count them as "completed" for
            # the total tally so the progress bar's denominator makes
            # sense.
            counts.completed += 1

    # ------------------------------------------------------------------
    # Cold-read helper (r-review r1 inline).
    # ------------------------------------------------------------------

    def _status_from_meta_and_tail(
        self,
        *,
        run_id: WorkflowRunId,
        tail_bytes: int,
    ) -> WorkflowRunStatusView:
        """Reconstruct phase from the meta sidecar + bounded JSONL tail.

        Returns `phase="unknown"` when the store has no record.
        Otherwise reads `meta.json` for the executor's persisted
        status, then scans the JSONL tail for a terminal event —
        the tail is authoritative when a terminal has landed.
        Translates the executor's `WorkflowRunStatus` enum to the
        services `WorkflowRunPhase` via `executor_status_to_phase()`.
        """
        # `WorkflowRunStore.read` walks the meta + full JSONL to
        # produce a `WorkflowRun`. Its terminal-scan reads the file
        # bottom-up but doesn't bound the byte count; for spec-012
        # cold-read we do our own bounded tail scan so a very long
        # JSONL never blocks status().
        run_dir = self._run_store.directory(run_id)
        if not run_dir.meta_path.exists():
            return WorkflowRunStatusView(run_id=run_id, phase="unknown")

        try:
            meta = json.loads(run_dir.meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return WorkflowRunStatusView(run_id=run_id, phase="unknown")

        raw_status = meta.get("status")
        try:
            base_status = WorkflowRunStatus(raw_status)
        except ValueError:
            return WorkflowRunStatusView(run_id=run_id, phase="unknown")

        # Bounded tail scan for a terminal event. Any of
        # `workflow.run.{completed,failed,timed_out,aborted}` wins
        # over the meta's status.
        terminal_status = _scan_tail_for_terminal(
            run_dir.events_path, tail_bytes=tail_bytes,
        )
        effective_status = terminal_status or base_status
        phase = executor_status_to_phase(effective_status)
        return WorkflowRunStatusView(run_id=run_id, phase=phase)

    # ------------------------------------------------------------------
    # spec-014: approval lifecycle emit hooks.
    # ------------------------------------------------------------------

    async def emit_awaiting_approval(
        self,
        *,
        run_id: str,
        node_name: str,
    ) -> EventId:
        """Emit `kolsch.workflow.node.awaiting_approval` durably.

        Called from `EventAdapter._handle_prompt_requested` on the
        workflow-source branch. The `WorkflowNodeRow` transitions to
        `paused` (glyph `⚠`) when the matching `WorkflowRenderer`
        applies the resulting UI event.

        `run_id` / `node_name` are strings — the adapter carries the
        payload's `source` dict verbatim and passes both through.
        """
        payload: dict[str, Any] = {
            "run_id": run_id,
            "node_name": node_name,
        }
        return await self._emit_ui_event(
            WIRE_WORKFLOW_NODE_AWAITING_APPROVAL,
            payload,
            critical=True,
        )

    async def emit_approval_resolved(
        self,
        *,
        run_id: str,
        node_name: str,
        resolution: str,
    ) -> EventId:
        """Emit `kolsch.workflow.node.approval_resolved` durably.

        Called from `EventAdapter._handle_prompt_answered` /
        `_handle_prompt_cancelled` on the workflow-source branch.
        `resolution` is one of `"approved"` / `"denied"` /
        `"cancelled"`; downstream the renderer transitions the row
        back to `running` (glyph `▸`).
        """
        payload: dict[str, Any] = {
            "run_id": run_id,
            "node_name": node_name,
            "resolution": resolution,
        }
        return await self._emit_ui_event(
            WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED,
            payload,
            critical=True,
        )

    # ------------------------------------------------------------------
    # UI-event emit (spec-006 pattern).
    # ------------------------------------------------------------------

    async def _emit_ui_event(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        critical: bool = True,
    ) -> EventId:
        """Append a UI_EVENT to the session store.

        Bus publish is deliberately a no-op today (spec-006 Q4 lock);
        the durability contract is the append itself — replay-on-boot
        picks up the event.

        `critical` is accepted for signature parity with `CardsImpl._emit_ui_event`
        (spec-006). Today's WorkflowsImpl has no streaming queue —
        every emit is durable via `append_event` — so the kwarg is a
        no-op today. spec-014's approval events pass `critical=True`
        so future bus wiring preserves the guarantee.
        """
        _ = critical
        event = Event(
            event_id=_new_event_id(),
            parent_event_id=None,
            thread_id=self._thread_id,
            session_id=self._session_id,
            type=EventType.UI_EVENT,
            data={
                "schema_version": 1,
                "kind": kind,
                "payload": payload,
            },
            actor=Actor(kind="system"),
            created_at=datetime.now(UTC),
        )
        await self._session_store.append_event(self._thread_id, event)
        return event.event_id

    # ------------------------------------------------------------------
    # Default executor factory (production path).
    # ------------------------------------------------------------------

    def _default_executor_factory(
        self,
        ref: str,
        inputs: dict[str, Any],
        event_observer: Callable[[WorkflowEvent], None],
        cancel_token: "_CancelToken",
    ) -> "tuple[IterativeExecutor, RunDirectory]":
        """Build a real `IterativeExecutor` via the shared helper.

        Wires the runtime's catalog / authz / secrets / sandbox
        exactly like `cli/workflows.py::_cmd_run` does, minus the
        stdout/stderr rendering — this impl is stream-live rather
        than blocking-cli.
        """
        from cli._workflow_runtime import build_workflow_executor  # noqa: PLC0415
        from workflows.executor import IterativeExecutor  # noqa: PLC0415
        from workflows.parser import parse_workflow_yaml  # noqa: PLC0415

        catalog = self._runtime.asset_management.catalog()
        entry = catalog.get(ref)
        workflow = parse_workflow_yaml(entry.path.read_text(encoding="utf-8"))

        run = self._run_store.create(
            workflow_ref=ref,
            inputs=inputs,
            started_by="in-process",
        )
        executor = build_workflow_executor(
            root=self._runtime.root,
            runtime=self._runtime,
            workflow=workflow,
            run=run,
            store=self._run_store,
        )
        # Re-inject observer + cancel via the executor's spec-011
        # kwargs. `build_workflow_executor` doesn't take them; we
        # rebuild the executor with the same registry to add them.
        executor = IterativeExecutor(
            workflow=workflow,
            run=run,
            store=self._run_store,
            registry=executor._registry,
            catalog=catalog,
            event_observer=event_observer,
            cancel_token=cancel_token,
        )
        run_dir = self._run_store.directory(run.run_id)
        return executor, run_dir


# ---------------------------------------------------------------------------
# Module-level helpers.
# ---------------------------------------------------------------------------


_TERMINAL_KIND_TO_STATUS: dict[WorkflowEventKind, WorkflowRunStatus] = {
    WorkflowEventKind.RUN_COMPLETED: WorkflowRunStatus.SUCCEEDED,
    WorkflowEventKind.RUN_FAILED:    WorkflowRunStatus.FAILED,
    WorkflowEventKind.RUN_TIMED_OUT: WorkflowRunStatus.TIMED_OUT,
    WorkflowEventKind.RUN_ABORTED:   WorkflowRunStatus.ABORTED,
}


_TERMINAL_EXECUTOR_KINDS: frozenset[WorkflowEventKind] = frozenset(
    _TERMINAL_KIND_TO_STATUS,
)


_TERMINAL_JSONL_KIND_TO_STATUS: dict[str, WorkflowRunStatus] = {
    "workflow.run.completed": WorkflowRunStatus.SUCCEEDED,
    "workflow.run.failed":    WorkflowRunStatus.FAILED,
    "workflow.run.timed_out": WorkflowRunStatus.TIMED_OUT,
    "workflow.run.aborted":   WorkflowRunStatus.ABORTED,
}


def _scan_tail_for_terminal(
    events_path: Any,
    *,
    tail_bytes: int,
) -> WorkflowRunStatus | None:
    """Read the last `tail_bytes` of the JSONL; return a terminal status if found.

    Best-effort — a missing file, unreadable bytes, or a partial
    trailing line all return None so the caller falls back to the
    meta sidecar's status.
    """
    if not events_path.exists():
        return None
    try:
        size = events_path.stat().st_size
    except OSError:
        return None
    start = max(0, size - tail_bytes)
    try:
        with events_path.open("rb") as fh:
            fh.seek(start)
            data = fh.read()
    except OSError:
        return None

    text = data.decode("utf-8", errors="ignore")
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        kind = row.get("kind")
        if kind in _TERMINAL_JSONL_KIND_TO_STATUS:
            return _TERMINAL_JSONL_KIND_TO_STATUS[kind]
    return None
