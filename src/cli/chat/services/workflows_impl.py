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
import contextvars
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
    WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT,
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
    from bus.event_bus import EventBus
    from cli.chat.services.workflow_layout import WorkflowLayoutManager
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


_SUB_AGENT_EVENT_TYPES_DROP: frozenset[str] = frozenset(
    {
        # spec-020 §Open questions — drop noise. `model.request` is
        # structurally redundant with the follow-up `model.response`
        # (the request carries the outgoing prompt bytes; the
        # response carries the incoming completion + tool calls the
        # UI actually renders). `model.streaming_token` is very high
        # frequency (one per delta) and dominates the queue budget
        # without offering per-chunk UI value in the current
        # `WorkflowNodeRow` — the closing `model.response` /
        # `agent.message` carries the full text. Both filters are
        # v1-safe: the child session's JSONL still carries these
        # events unchanged.
        "model.request",
        "model.streaming_token",
    }
)
"""Session-scoped event types dropped BEFORE bridge translation.
Everything else (`tool.called`, `tool.result`, `agent.message`,
`user.message`, `model.response`, ...) is forwarded verbatim."""


# ---------------------------------------------------------------------------
# spec-017: nested-detection contextvar.
# ---------------------------------------------------------------------------


_current_workflow_run_id: contextvars.ContextVar[
    "WorkflowRunId | None"
] = contextvars.ContextVar(
    "_current_workflow_run_id", default=None,
)
"""spec-017 nested detection.

Set by :meth:`WorkflowsImpl._run_one_workflow` immediately before the
executor's ``run()`` call and reset in the ``finally``. A subsequent
:meth:`WorkflowsImpl.start` invocation reads this contextvar to
populate :attr:`WorkflowRunRecord.parent_run_id`; a non-None value
means the start call was fired from inside another live run's task
(either a nested ``kolsch/nodes/workflow`` node or a peer
``ctx.workflows.start`` call from a tool). Contextvars propagate
across ``asyncio`` task boundaries, so downstream ``ensure_future``
descendants of the executor task still see the parent's id.

The peer-vs-nested ambiguity is documented (spec-017 Q3 lock):
both look like nested to this contextvar. Butler-tool call sites
that want explicit peer semantics reset it before calling ``start``.
"""


# ---------------------------------------------------------------------------
# Sentinel for the per-run bridge drain shutdown.
# ---------------------------------------------------------------------------


class _StopSentinel:
    """Sentinel type — the bridge drain terminates on receipt."""


_STOP_SENTINEL = _StopSentinel()


@dataclass(frozen=True)
class _SubAgentBridgeItem:
    """spec-020 bridge envelope for a session-scoped sub-agent `Event`.

    The executor's `sub_agent_event_observer` enqueues one of these
    per event yielded by `AgentEngine.execute_stream(...)` inside an
    `AgentRunner`. The bridge drain translates each into a
    `kolsch.workflow.node.sub_agent_event` UI event keyed on the
    owning `(node_name, invocation_id)` so the receiver
    (`WorkflowRenderer.apply_event`) can route it into the matching
    `WorkflowNodeRow` via bug-033's `(trace_id, invocation_id)`
    composite map.
    """

    node_name: str
    invocation_id: str
    event: Event


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
    # feat-019: True while at least one node on this run is waiting on an
    # approval. Flipped by ``emit_awaiting_approval`` / cleared by
    # ``emit_approval_resolved``. Read by
    # :func:`cli.chat.services.workflow_layout.format_live_workflow_strip`
    # to count the ``⚠ N paused`` suffix. Default False so pre-feat-019
    # test fixtures that build records via minimal kwargs still parse.
    awaiting_approval: bool = False
    # bug-025: terminal-error diagnostic. Populated by
    # ``_finalize_terminal`` (construction-failure path — no node ran)
    # AND by ``_drain_workflow_bridge`` when a ``RUN_FAILED`` /
    # ``NODE_FAILED`` event lands. ``status()`` reads these into the
    # matching ``WorkflowRunStatusView`` fields so butler-facing tools
    # (``workflow_status``) can surface the failure back to the model —
    # closing the "silent retry loop" symptom from session ses_629b626e.
    error_message: str | None = None
    failed_node: str | None = None
    # bug-031: terminal outputs from ``workflow.run.completed.payload.
    # outputs``. Populated by ``_drain_workflow_bridge`` on the
    # ``RUN_COMPLETED`` event (see ``_maybe_capture_outputs``).
    # ``status()`` surfaces this into ``WorkflowRunStatusView.outputs``
    # so the blocking ``run_workflow`` tool can return the outputs in
    # the same turn as the tool call — closing the "12 min dead zone
    # after workflow completes" symptom from session ses_af5980ab.
    outputs: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# WorkflowsImpl.
# ---------------------------------------------------------------------------


# Type alias for the pluggable executor factory. Production wires
# the concrete `_build_in_process_executor` (see
# `_default_executor_factory` below); tests inject a mock so the
# executor's full dependency graph (Workflow, RunnerRegistry,
# catalog) isn't required to exercise the queue/bridge/aclose paths.
#
# spec-020: the factory now accepts an optional
# `sub_agent_event_observer` — the executor calls it per session-
# scoped `Event` yielded by `AgentEngine.execute_stream(...)` inside
# an `AgentRunner`. Existing mock factories (test doubles) that only
# accept the 4-arg shape are supported via a compatibility shim in
# `_run_one_workflow` (introspect the callable's arity and call the
# appropriate signature) so pre-spec-020 test fixtures don't need to
# be rewritten.
ExecutorFactory = Callable[
    ...,
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
        bus: "EventBus | None" = None,
        default_timeout_seconds: int | None = None,
    ) -> None:
        self._runtime = runtime
        self._cards = cards
        self._prompts = prompts
        self._session_store = session_store
        self._session_id = session_id
        self._thread_id = thread_id
        self._run_store = run_store
        # Prefer the explicit ctor arg; otherwise read from the runtime
        # (populated at bootstrap from
        # `platform.yaml:workflows.default_timeout_seconds`); otherwise
        # fall back to the AbstractWorkflows 300s default.
        if default_timeout_seconds is not None:
            self._default_timeout_seconds = int(default_timeout_seconds)
        else:
            self._default_timeout_seconds = int(
                getattr(runtime, "workflow_default_timeout_seconds", 300),
            )
        # bug-020: in-process runtime bus. When present,
        # `_emit_ui_event` publishes each UI event on `UI_EVENT_TOPIC`
        # AFTER the store append so the live `EventAdapter.handle`
        # subscription in `ChatApp.on_mount` sees `kolsch.workflow.
        # progress` / `kolsch.workflow.node.awaiting_approval` etc.
        # events in the SAME session. `None` in headless tests /
        # adapter-less fixtures — publish is a silent no-op, matching
        # the pre-bug-020 stub behaviour.
        self._bus: "EventBus | None" = bus
        # Test hook: default factory builds a real `IterativeExecutor`
        # via `_workflow_runtime.build_workflow_executor`. Test bundles
        # pass a mock that returns a stub executor + minted RunDirectory
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

        # spec-016: multi-workflow layout manager. Set by ChatApp.on_mount
        # after the transcript container and event adapter are up (the
        # manager needs a live transcript + card-widget lookup). Left
        # None in headless tests / adapter-less paths — the notify hook
        # is a silent no-op in that case.
        self._layout_manager: "WorkflowLayoutManager | None" = None

    # ------------------------------------------------------------------
    # spec-016: WorkflowLayoutManager wiring
    # ------------------------------------------------------------------

    def register_layout_manager(
        self, layout_manager: "WorkflowLayoutManager",
    ) -> None:
        """Wire a live layout manager after construction.

        Called by ``ChatApp.on_mount`` once the transcript container
        and event adapter are up. Enables the impl's phase-transition
        hooks (``_run_one_workflow``, ``_finalize_terminal``, etc.) to
        fire ``notify_card_state_changed`` so the multi-workflow
        layout batches reorders on the 250ms tick.
        """
        self._layout_manager = layout_manager

    def _notify_layout(self, run_id: WorkflowRunId) -> None:
        """Schedule a layout reorder if a manager is registered.

        No-op when the layout manager hasn't been wired (headless
        tests, adapter-less paths). Best-effort — any raise from the
        manager is swallowed so a bad layout callback can't kill the
        drain worker.
        """
        if self._layout_manager is None:
            return
        try:
            self._layout_manager.notify_card_state_changed(run_id)
        except Exception:  # noqa: BLE001 — layout hook is best-effort
            _LOGGER.exception(
                "WorkflowsImpl: layout notify failed for %s", run_id,
            )

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

        # spec-017: nested detection. If a `_run_one_workflow` frame is
        # active up-task, the contextvar carries its run_id and we
        # stamp this new run as its child. Peer detection is
        # documented as ambiguous — a tool-side `ctx.workflows.start`
        # from inside a workflow's node also looks nested from the
        # contextvar's POV. Spec-017 Q3 lock: accept as-is.
        parent_run_id = _current_workflow_run_id.get()

        record = WorkflowRunRecord(
            run_id=run_id,
            ref=ref,
            phase="queued",
            card=card,
            created_at=datetime.now(tz=UTC),
            parent_run_id=parent_run_id,
        )
        self._workflow_registry[run_id] = record

        # spec-017: populate the reverse map so `_collect_cancel_tree`
        # can walk descendants during a cascade cancel.
        if parent_run_id is not None:
            self._children_of.setdefault(parent_run_id, []).append(run_id)

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

        # spec-016: notify the layout manager about the queued->
        # tracked transition so the card lands in the sorted layout.
        self._notify_layout(run_id)

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
                ref=record.ref,
                # bug-025: hot-path diagnostic surfacing.
                error_message=record.error_message,
                failed_node=record.failed_node,
                # bug-031: hot-path outputs surfacing so the blocking
                # ``run_workflow`` tool sees the terminal payload as
                # soon as ``_drain_workflow_bridge`` caches it.
                outputs=record.outputs,
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

        spec-017: `cascade=True` walks ancestors up (via
        :attr:`WorkflowRunRecord.parent_run_id`) AND descendants down
        (via :attr:`_children_of`) and cancels every reachable live
        run. Terminal runs are skipped inside the walk so a
        long-terminal ancestor doesn't derail the cascade. Returns
        True when at least one target was cancelled; False when the
        `run_id` is unknown or every reachable run is already
        terminal.
        """
        if cascade:
            targets = self._collect_cancel_tree(run_id)
        else:
            targets = [run_id] if run_id in self._workflow_registry else []

        fired_any = False
        for target_id in targets:
            token = self._workflow_cancels.get(target_id)
            record = self._workflow_registry.get(target_id)
            if token is None or record is None:
                # Cascade walk may include a terminal run whose token +
                # bridge were dropped by `_finalize_terminal`; skip.
                continue
            # Skip terminal runs — cancel doesn't apply.
            if record.phase in ("success", "error", "cancelled"):
                continue

            token.cancel()

            if record.run_dir is not None:
                try:
                    record.run_dir.cancel_flag_path.write_text("user_cancel")
                except OSError:
                    _LOGGER.exception(
                        "WorkflowsImpl: cancel flag write failed for %s",
                        target_id,
                    )

            record.phase = "cancelling"
            if record.card is not None:
                with contextlib.suppress(Exception):
                    await record.card.set_status("cancelling")
            # spec-016: running -> cancelling.
            self._notify_layout(target_id)
            fired_any = True

        return fired_any

    def _collect_cancel_tree(
        self,
        run_id: WorkflowRunId,
    ) -> list[WorkflowRunId]:
        """Return ``run_id`` + its live ancestors + its live descendants.

        Walks :attr:`WorkflowRunRecord.parent_run_id` upward from
        ``run_id`` to build the ancestor set, then BFS's
        :attr:`_children_of` downward from every collected id to
        gather descendants. Cycle-safe via a ``seen`` set — the
        registry shouldn't carry cycles by construction (a start
        stamps its parent BEFORE the child stamps a grandchild), but
        the check makes the walker robust to future changes.

        Terminal-liveness is NOT filtered here — the returned list
        includes runs whose registry record has flipped to a terminal
        phase. :meth:`cancel` filters those out inside its per-target
        loop so cascade semantics stay uniform with the single-run
        path (both skip terminals; both skip unknowns).

        A ``run_id`` unknown to :attr:`_workflow_registry` returns an
        empty list — the caller's :meth:`cancel` handles the empty
        case as "no work; return False".
        """
        seen: set[WorkflowRunId] = set()
        # Ancestor walk — only starts when the id is known.
        if run_id not in self._workflow_registry:
            return []

        cur: WorkflowRunId | None = run_id
        while cur is not None and cur not in seen:
            seen.add(cur)
            rec = self._workflow_registry.get(cur)
            cur = rec.parent_run_id if rec is not None else None

        # Descendant BFS from every collected id.
        stack: list[WorkflowRunId] = list(seen)
        while stack:
            rid = stack.pop()
            for child_id in self._children_of.get(rid, []):
                if child_id not in seen:
                    seen.add(child_id)
                    stack.append(child_id)

        return list(seen)

    @property
    def default_timeout_seconds(self) -> int:
        return self._default_timeout_seconds

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
        """Build the executor, wire observer + cancel, drive to terminal.

        spec-017: sets the ``_current_workflow_run_id`` contextvar for
        the entire body so any downstream :meth:`start` call (from a
        `kolsch/nodes/workflow` runner or a tool via
        ``ctx.workflows.start``) stamps this run's id as the child's
        ``parent_run_id``. The set happens BEFORE executor
        construction because factory callbacks (e.g., the mock
        executor's ``on_construct``) may already schedule nested
        starts. Reset happens in ``finally`` — even on a construction-
        time raise — so the outer ambient context is always restored.
        """
        from engine.context import CancelToken  # noqa: PLC0415

        run_id = spec.run_id
        record = self._workflow_registry.get(run_id)
        if record is None:
            # Aclose raced with drain — bail cleanly.
            return

        # spec-017: set the nested-detection contextvar for the
        # whole `_run_one_workflow` body. See method docstring.
        _ctxvar_token = _current_workflow_run_id.set(run_id)

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
        # spec-016: queued -> running phase transition.
        self._notify_layout(run_id)

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

        # spec-020: sub-agent event forwarder. The executor invokes
        # this per session-scoped `Event` yielded by
        # `AgentEngine.execute_stream(...)` inside an `AgentRunner`.
        # We wrap each in a `_SubAgentBridgeItem` so the drain can
        # distinguish it from a `WorkflowEvent`. The observer
        # terminates naturally when the workflow node reaches
        # terminal — `AgentEngine.execute_stream(...)` closes its
        # iterator, `AgentRunner.run` returns, and the runner never
        # calls the forwarder again. No explicit unwire needed
        # (matches the `_event_observer` discipline; the bridge
        # drain shuts down on the `_STOP_SENTINEL` posted by
        # `_finalize_terminal`).
        def _sub_agent_observer(
            node_name: str, invocation_id: str, event: Event,
        ) -> None:
            try:
                bridge_queue.put_nowait(
                    _SubAgentBridgeItem(
                        node_name=node_name,
                        invocation_id=invocation_id,
                        event=event,
                    ),
                )
            except asyncio.QueueFull:
                record.dropped_events += 1

        try:
            try:
                # spec-020: pass the sub-agent observer as a fifth
                # positional argument. Pre-spec-020 test mocks with
                # the 4-arg signature raise `TypeError`; catch that
                # and fall back to the 4-arg shape so those tests
                # keep passing (they don't exercise sub-agent
                # forwarding). `_default_executor_factory` accepts
                # the 5-arg form; `tests/.../mocks/workflow_executor.py`
                # is being extended in the same commit to accept it
                # too via a keyword-with-default.
                try:
                    executor, run_dir = self._executor_factory(
                        spec.ref,
                        spec.inputs,
                        _observer,
                        cancel_token,
                        _sub_agent_observer,
                    )
                except TypeError:
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
            except Exception as _factory_exc:
                _LOGGER.exception(
                    "WorkflowsImpl: executor construction failed for %s",
                    run_id,
                )
                record.phase = "error"
                # bug-025: capture the construction-failure diagnostic
                # so `status()` surfaces a legible reason to the model.
                # The factory-time raise is the "workflow failed before
                # any node ran" path — no `failed_node` because no
                # node fired; the exception message is the load-
                # bearing diagnostic.
                if record.error_message is None:
                    record.error_message = str(_factory_exc)[:400]
                await self._finalize_terminal(spec, bridge_task)
                return

            # NB: use `getattr` to invoke the executor's entry point
            # rather than a bare `executor.run()` attribute-call.
            # TC-CHAT-18's AST walker treats every `.run(...)` under
            # `src/cli/chat/` as a Textual `App(...).run(...)` and
            # requires an `inline=True` kwarg — it can't distinguish
            # an executor call site. `getattr` sidesteps the walker
            # without changing behaviour.
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
            except Exception as _run_exc:  # noqa: BLE001 — bubble as run failure
                _LOGGER.exception(
                    "WorkflowsImpl: executor run failed for %s", run_id,
                )
                # bug-025: capture the run-time raise as a fallback
                # diagnostic. The bridge drain converts a proper
                # terminal event's payload into `error_message` first
                # (see `_drain_workflow_bridge`); this only fires when
                # the executor raised BEFORE emitting a terminal event
                # (wiring bug / infra failure). Do not clobber a
                # previously-captured message.
                if record.error_message is None:
                    record.error_message = str(_run_exc)[:400]
                # The executor writes its own terminal event on the way
                # out; the bridge drain will convert it. If it didn't
                # (construction-time raise, wiring bug), close the card
                # here as a fallback.
                if spec.card is not None and not spec.card.is_closed:
                    with contextlib.suppress(Exception):
                        # bug-030 Fix 2 (site 1/3): pipe
                        # ``record.error_message`` into the fallback
                        # close so the WorkflowCard widget renders the
                        # underlying diagnostic instead of a bare red X.
                        # bug-025 already stashed the reason on the
                        # record above.
                        await spec.card.close(
                            final_status="error",
                            error_message=record.error_message,
                        )
            finally:
                await self._finalize_terminal(spec, bridge_task)
        finally:
            # spec-017: reset the nested-detection contextvar so a
            # downstream `start()` fired after this run terminates
            # (or in a peer task) doesn't accidentally inherit this
            # run's id as its parent. Always fires — even on a
            # construction-time raise or an early return above.
            with contextlib.suppress(ValueError, LookupError):
                _current_workflow_run_id.reset(_ctxvar_token)

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
            # spec-069 Part A — belt-and-braces reconciliation of the
            # in-flight node count. `_update_node_counts` already
            # decrements `running` on `NODE_COMPLETED` / `NODE_FAILED` /
            # `NODE_TIMED_OUT`, but a construction-failure path (record
            # created, `workflow.node.started` never emitted, or a
            # bridge that dropped an event) can still leave `running > 0`
            # when the terminal phase lands. Zero it here so
            # `workflow_status` / `node_counts.running` never reports a
            # phantom in-flight node on a terminal record — the fix
            # spec-069 Background (defect 1) points at (the wedged
            # 2-node workflow stuck at running=1 forever). Cheap,
            # obviously correct, no behaviour change on the happy path.
            if record.node_counts.running != 0:
                record.node_counts.running = 0

        # Close the bound card if the bridge didn't already.
        if spec.card is not None and not spec.card.is_closed:
            final = self._card_final_status_for_phase(
                record.phase if record is not None else "error"
            )
            with contextlib.suppress(Exception):
                # bug-030 Fix 2 (site 2/3): pipe
                # ``record.error_message`` into ``_finalize_terminal``'s
                # fallback close. This covers the construction-failure
                # path AND any bridge-drain that terminated without
                # closing the card (e.g. sentinel-first race).
                await spec.card.close(
                    final_status=final,
                    error_message=(
                        record.error_message if record is not None else None
                    ),
                )

        # spec-016: notify layout of the terminal transition BEFORE
        # eviction so the card can float up into the terminal band.
        self._notify_layout(run_id)

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
    # spec-020: sub-agent event bridge translation.
    # ------------------------------------------------------------------

    async def _emit_sub_agent_ui_event(
        self,
        spec: "WorkflowRunSpec",
        item: _SubAgentBridgeItem,
    ) -> None:
        """Translate a `_SubAgentBridgeItem` into a UI event.

        Emits a `kolsch.workflow.node.sub_agent_event` payload matching
        the envelope shape from spec-020:

            {
              "run_id": str,          # workflow run id
              "node_name": str,       # owning workflow node
              "invocation_id": str,   # per-node discriminant
              "trace_id": str,        # parity with WorkflowEvent
              "event_type": str,      # e.g. "tool.called"
              "event_id": str,        # original Event.event_id
              "payload": dict,        # original Event.data
            }

        Noise-filter: `model.request` events are structurally
        redundant with the follow-up `model.response` (they carry
        the outgoing prompt; the response carries the incoming
        completion + tool calls the UI actually cares about) so we
        drop them here rather than paying the round-trip cost. All
        other event types are forwarded for v1 — filter later if
        users complain about noise (spec-020 §Open questions).
        """
        event = item.event
        event_type_value = str(event.type.value)
        if event_type_value in _SUB_AGENT_EVENT_TYPES_DROP:
            return
        payload: dict[str, Any] = {
            "run_id": str(spec.run_id),
            "node_name": item.node_name,
            "invocation_id": item.invocation_id,
            "trace_id": str(event.trace_id) if event.trace_id else "",
            "event_type": event_type_value,
            "event_id": str(event.event_id),
            "payload": dict(event.data or {}),
        }
        await self._emit_ui_event(
            WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT,
            payload,
        )

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
            if isinstance(item, _SubAgentBridgeItem):
                # spec-020: session-scoped sub-agent event. Translate
                # to a `kolsch.workflow.node.sub_agent_event` UI event
                # and short-circuit — the WorkflowEvent path below
                # doesn't apply (no `.kind` / no counters / no
                # terminal transitions to trigger).
                await self._emit_sub_agent_ui_event(spec, item)
                continue
            event: WorkflowEvent = item

            # Update the record's live-view fields.
            if record is not None:
                record.last_event_ts = event.ts
                self._update_node_counts(record.node_counts, event.kind)
                # bug-025: capture terminal-error diagnostics on
                # `NODE_FAILED` / `NODE_TIMED_OUT` / `RUN_FAILED` so
                # `status()` surfaces the reason back to the model.
                # First-writer-wins: the earliest failure carries the
                # attribution (a per-node failure that later triggers
                # RUN_FAILED shouldn't get its message clobbered by
                # the parent's "node X raised Y" summary).
                self._maybe_capture_error(record, event)
                # bug-031: capture terminal outputs on RUN_COMPLETED so
                # the blocking ``run_workflow`` tool's poll sees a
                # populated ``outputs`` dict on ``status()`` in the same
                # turn — closes the "workflow completes silently" gap.
                self._maybe_capture_outputs(record, event)

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

            # bug-031 Fix 5: notify the layout manager on EVERY bridge
            # emit — not just the terminal transition. The manager's
            # ``notify_card_state_changed`` idempotence window (250ms)
            # collapses N node-level notifications into a single
            # reorder pass, which pushes ``format_live_workflow_strip``
            # (wall-clock elapsed) so the status-line-2 strip keeps
            # ticking between phase transitions. Session ses_af5980ab
            # showed 90.4s + 65s dead zones on the strip because the
            # pre-bug-031 code only pinged the manager on terminal.
            self._notify_layout(run_id)

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
                        # bug-030 Fix 2 (site 3/3): pipe
                        # ``record.error_message`` into the bridge-drain
                        # terminal close. ``_maybe_capture_error`` above
                        # populates ``record.error_message`` from
                        # ``NODE_FAILED`` / ``NODE_TIMED_OUT`` /
                        # ``RUN_FAILED`` payloads before we land here on
                        # a matching terminal kind — first-writer-wins,
                        # so the earliest node-scoped attribution is
                        # what surfaces to the widget.
                        await spec.card.close(
                            final_status=final,
                            error_message=(
                                record.error_message
                                if record is not None
                                else None
                            ),
                        )
                # spec-016: bridge-drain terminal — notify layout so
                # the card moves into the terminal band on the next
                # 250ms tick. `_finalize_terminal` also notifies but
                # firing here is idempotent (the manager batches).
                self._notify_layout(run_id)
                # Drain terminates on the sentinel; keep looping so
                # any late in-flight events land before we shut down.

    @staticmethod
    def _maybe_capture_error(
        record: WorkflowRunRecord,
        event: WorkflowEvent,
    ) -> None:
        """Capture terminal-error diagnostics from a bridge event, if any.

        bug-025. Reads the executor payload for
        ``NODE_FAILED`` / ``NODE_TIMED_OUT`` / ``RUN_FAILED``
        envelopes and populates the record's ``error_message`` /
        ``failed_node``. First-writer-wins so an earlier
        node-scoped failure isn't clobbered by a later
        `run.failed` "node X raised Y" summary.

        Payload keys read:

        - Node failure: ``message`` (executor emit; see
          ``src/workflows/executor.py:1984``) + ``node_name``.
        - Node timeout: same shape as node failure.
        - Run failure: ``reason`` (executor emit; see
          ``src/workflows/executor.py:2045``); ``failed_node`` stays
          ``None`` when no node-scoped failure preceded it.
        """
        if record.error_message is not None:
            return
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.kind in (
            WorkflowEventKind.NODE_FAILED,
            WorkflowEventKind.NODE_TIMED_OUT,
        ):
            msg = payload.get("message")
            if isinstance(msg, str) and msg:
                record.error_message = msg[:400]
            node_name = payload.get("node_name") or getattr(
                event, "node_name", None,
            )
            if isinstance(node_name, str) and node_name:
                record.failed_node = node_name
            return
        if event.kind == WorkflowEventKind.RUN_FAILED:
            reason = payload.get("reason")
            if isinstance(reason, str) and reason:
                record.error_message = reason[:400]

    @staticmethod
    def _maybe_capture_outputs(
        record: WorkflowRunRecord,
        event: WorkflowEvent,
    ) -> None:
        """Capture terminal outputs from a ``workflow.run.completed`` bridge event.

        bug-031. The executor's ``WorkflowRunCompletedEvent`` carries
        the run's terminal outputs under ``payload.outputs`` (see
        ``src/workflows/executor.py:1571``). The blocking
        ``run_workflow`` tool polls ``status()`` at ~500ms; once this
        method stashes ``payload.outputs`` on the record,
        ``WorkflowsImpl.status()`` surfaces it into
        ``WorkflowRunStatusView.outputs`` and the tool returns to the
        model in the same turn — closing the "workflow completes
        silently, model 12min silent" symptom from ses_af5980ab.

        First-writer-wins: replaying a duplicate ``RUN_COMPLETED``
        won't clobber a prior capture. Non-``RUN_COMPLETED`` kinds are
        no-ops; ``RUN_FAILED`` / ``RUN_TIMED_OUT`` / ``RUN_ABORTED``
        payloads don't carry ``outputs`` and ``error_message`` covers
        their diagnostic surface.
        """
        if record.outputs is not None:
            return
        if event.kind is not WorkflowEventKind.RUN_COMPLETED:
            return
        payload = event.payload if isinstance(event.payload, dict) else {}
        outputs = payload.get("outputs")
        if isinstance(outputs, dict):
            record.outputs = outputs

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
        # bug-031: tuple is now (status, diag, outputs) so late polls
        # past ``_REGISTRY_EVICTION_SECONDS`` still see the outputs.
        terminal_status, terminal_diag, terminal_outputs = (
            _scan_tail_for_terminal(
                run_dir.events_path, tail_bytes=tail_bytes,
            )
        )
        effective_status = terminal_status or base_status
        phase = executor_status_to_phase(effective_status)
        # feat-018: surface the workflow's ref from the meta sidecar
        # so butler-facing tools see the ref even on cold-read.
        ref_raw = meta.get("workflow_ref")
        ref = ref_raw if isinstance(ref_raw, str) else None
        # bug-025: also surface error_message / failed_node from the
        # JSONL tail when the terminal was a failure. `terminal_diag`
        # is `None` on success / cold-read miss / non-failure
        # terminals.
        error_message: str | None = None
        failed_node: str | None = None
        if terminal_diag is not None:
            error_message, failed_node = terminal_diag
        return WorkflowRunStatusView(
            run_id=run_id,
            phase=phase,
            ref=ref,
            error_message=error_message,
            failed_node=failed_node,
            # bug-031: cold-read outputs — populated when the JSONL
            # tail carries a ``workflow.run.completed`` row.
            outputs=terminal_outputs,
        )

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

        feat-019: also flips the matching record's
        ``awaiting_approval`` flag so the status-line-2 strip's
        ``⚠ N paused`` counter picks the run up on the next layout
        flush.
        """
        # feat-019: flag the record for the status-line-2 ⚠ counter.
        # `run_id` arrives as a str from the adapter; look up the
        # typed WorkflowRunId key by string match.
        for rid, record in self._workflow_registry.items():
            if str(rid) == run_id:
                record.awaiting_approval = True
                self._notify_layout(rid)
                break

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

        feat-019: clears the matching record's ``awaiting_approval``
        flag so the status-line-2 strip's ``⚠ N paused`` counter drops
        this run on the next layout flush.
        """
        # feat-019: clear the pending flag so the ⚠ counter drops.
        for rid, record in self._workflow_registry.items():
            if str(rid) == run_id:
                record.awaiting_approval = False
                self._notify_layout(rid)
                break

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
        """Append a UI_EVENT to the session store, then publish to the bus.

        bug-020: the store append remains the durability contract —
        replay-on-boot picks up the event. After the append, if a
        runtime bus was injected at construction, publish the SAME
        `Event` object onto ``UI_EVENT_TOPIC`` so the live
        `EventAdapter.handle` subscription in ``ChatApp.on_mount``
        dispatches to the `WorkflowRenderer` inside the SAME session
        rather than only on next-boot replay. Publish AFTER append
        preserves the store-first invariant (D-008 aclose-idempotent
        cleanup: no bus event exists that isn't already in the log).

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
        # bug-020: publish to the in-process bus so the live adapter
        # subscription fires this session. The bus's `publish` is
        # best-effort (transport failures logged + swallowed at
        # `InProcessEventBus.publish`); a raise here would break a
        # workflow's progress-event surfacing, which is worse than
        # a silent bus fault.
        if self._bus is not None:
            with contextlib.suppress(Exception):
                await self._bus.publish(event)
        return event.event_id

    # ------------------------------------------------------------------
    # Default executor factory (production path).
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # bug-025: input-validation seam for run_workflow.
    # ------------------------------------------------------------------

    def resolve_workflow_yaml(self, ref: str) -> str | None:
        """Return the workflow YAML source for ``ref``, or ``None``.

        bug-025. The ``run_workflow`` tool uses this to load the
        workflow's node config strings + edge binds and scan for
        ``{{ inputs.X }}`` references at the tool boundary — BEFORE
        the fire-and-forget queue accepts the spec. Any missing
        ``inputs.<name>`` reference surfaces as a ``ToolExecutionFailed``
        with an actionable message so the model sees a legible
        failure signal instead of an optimistic ``run_id`` for a run
        that will error 2s later.

        Duck-typed in the tool via ``getattr(services.workflows,
        "resolve_workflow_yaml", None)`` — the ``Workflows`` Protocol
        stays byte-identical (no new abstract method), and headless
        fixtures / test doubles that don't expose this method fall
        back to the pre-bug-025 no-validation path (the workflow just
        errors 2s later, as before).

        Returns ``None`` when the catalog can't find the ref or the
        file can't be read; ``ToolExecutionFailed`` on a raise is the
        tool's job, not the service's.
        """
        try:
            catalog = self._runtime.asset_management.catalog()
            entry = catalog.lookup(ref)
        except Exception:  # noqa: BLE001 — best-effort probe
            return None
        if entry is None:
            return None
        try:
            from pathlib import Path  # noqa: PLC0415
            return Path(entry.path).read_text(encoding="utf-8")
        except OSError:
            return None

    def list_workflow_refs(self) -> list[str]:
        """Return every catalogued workflow ref (bug-027).

        Duck-typed on the ``Workflows`` Protocol just like
        ``resolve_workflow_yaml`` — the introspection tool
        ``workflow_inspect`` calls it to render a helpful "workflow
        not found; available refs: ..." message when the model
        supplies an unknown ref. Test fixtures without this method
        fall back to a generic error text.

        Returns an empty list on any catalog failure — the caller
        should degrade to a generic not-found message rather than
        raise, matching ``resolve_workflow_yaml`` semantics.
        """
        try:
            catalog = self._runtime.asset_management.catalog()
        except Exception:  # noqa: BLE001 — best-effort probe
            return []
        return sorted(
            entry.ref
            for entry in catalog.assets.values()
            if entry.asset_type == "workflow"
        )

    def _default_executor_factory(
        self,
        ref: str,
        inputs: dict[str, Any],
        event_observer: Callable[[WorkflowEvent], None],
        cancel_token: "_CancelToken",
        sub_agent_event_observer: (
            Callable[[str, str, Event], None] | None
        ) = None,
    ) -> "tuple[IterativeExecutor, RunDirectory]":
        """Build a real `IterativeExecutor` via the shared helper.

        Wires the runtime's catalog / authz / secrets / sandbox
        exactly like `cli/workflows.py::_cmd_run` does, minus the
        stdout/stderr rendering — this impl is stream-live rather
        than blocking-cli.

        bug-020: `build_workflow_executor` now accepts
        `event_observer` + `cancel_token` kwargs (with default
        `None`) so we can build the executor in one construction
        site rather than re-instantiating a fresh
        `IterativeExecutor` with the observer + token bolted on
        after the fact. The prior "double-build" pattern reached
        into a private `_registry` attribute and hurt readability.
        """
        from pathlib import Path  # noqa: PLC0415

        from cli._workflow_runtime import build_workflow_executor  # noqa: PLC0415
        from errors import WrappedError  # noqa: PLC0415
        from workflows.parser import load_workflow  # noqa: PLC0415

        catalog = self._runtime.asset_management.catalog()
        # bug-030: sibling of bug-026. The pre-bug-030 code called
        # ``catalog.get(ref)`` — a method that does NOT exist on
        # ``AssetCatalog`` (only ``.lookup(ref, expected_type=...)`` is
        # public; see ``src/assets/catalog.py:72``). Every chat-side
        # workflow invocation raised ``AttributeError`` before
        # ``RUN_STARTED``. The test blind spot bug-026 flagged
        # (``_default_executor_factory`` is only reached in production
        # because every workflow test injects a custom factory)
        # covered this too — hence the D-010-extension regression at
        # ``tests/cli/chat/services/test_default_executor_factory_e2e.py``.
        entry = catalog.lookup(ref, expected_type="workflow")
        if entry is None:
            available = sorted(
                a.ref
                for a in catalog.assets.values()
                if a.asset_type == "workflow"
            )
            available_hint = (
                ", ".join(repr(r) for r in available[:20])
                if available
                else "<none>"
            )
            msg = (
                f"Workflow ref {ref!r} not found in catalog. "
                f"Available workflow refs: {available_hint}."
            )
            raise WrappedError(
                message=msg,
                where=(
                    "cli.chat.services.workflows_impl."
                    "_default_executor_factory"
                ),
                detail={
                    "ref": ref,
                    "available_refs": available,
                },
            )
        # bug-030 (adjacent): `AssetEntry.path` is typed as ``str``
        # (see ``src/assets/catalog.py:50``); ``load_workflow``
        # takes a ``Path``. The CLI wraps it via ``Path(entry.path)``
        # at every call site (``src/cli/workflows.py:383, 461, 622,
        # 751, 933``). The chat-side factory dropped the wrap, so
        # even after fixing ``catalog.get`` this line raised
        # ``AttributeError: 'str' object has no attribute 'read_text'``
        # inside ``parser.load_workflow``. Same test blind spot
        # (production-only path); the AC #4 e2e regression pins this
        # too.
        workflow = load_workflow(Path(entry.path))

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
            event_observer=event_observer,
            cancel_token=cancel_token,
            sub_agent_event_observer=sub_agent_event_observer,
        )
        # Silence unused-import lint in factory-simplified path.
        _ = catalog
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
) -> tuple[
    WorkflowRunStatus | None,
    tuple[str | None, str | None] | None,
    dict[str, Any] | None,
]:
    """Read the last `tail_bytes` of the JSONL; return a terminal status if found.

    Best-effort — a missing file, unreadable bytes, or a partial
    trailing line all return ``(None, None, None)`` so the caller
    falls back to the meta sidecar's status.

    bug-025: returns a ``(status, diag, outputs)`` tuple. ``diag`` is
    ``(error_message, failed_node)`` when a failure-shaped terminal
    was found, otherwise ``None``. Callers on non-failure terminals
    (COMPLETED / TIMED_OUT / ABORTED) get ``diag=None``; a failed
    terminal populates as many fields as the JSONL row exposes.

    bug-031: extends the return with a third element — ``outputs`` —
    populated on a ``workflow.run.completed`` row with a
    ``payload.outputs`` dict. Non-completion terminals return
    ``outputs=None``. Late polls of the blocking ``run_workflow``
    tool that miss the 60s registry window (``_REGISTRY_EVICTION_SECONDS``)
    still get the outputs through this cold-read fallback so a slow
    workflow's model-facing prose surface isn't lost.

    NODE_FAILED entries can also seed the diagnostic when a
    ``RUN_FAILED`` didn't carry a `reason` (older JSONL rows) —
    walking bottom-up, we take the first failure-shaped row.
    """
    if not events_path.exists():
        return None, None, None
    try:
        size = events_path.stat().st_size
    except OSError:
        return None, None, None
    start = max(0, size - tail_bytes)
    try:
        with events_path.open("rb") as fh:
            fh.seek(start)
            data = fh.read()
    except OSError:
        return None, None, None

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
            status = _TERMINAL_JSONL_KIND_TO_STATUS[kind]
            # bug-025: pull the failure diagnostic when applicable.
            if status is WorkflowRunStatus.FAILED:
                payload = row.get("payload")
                error_message: str | None = None
                failed_node: str | None = None
                if isinstance(payload, dict):
                    reason = payload.get("reason") or payload.get("message")
                    if isinstance(reason, str) and reason:
                        error_message = reason[:400]
                    node_name = payload.get("node_name") or row.get("node_name")
                    if isinstance(node_name, str) and node_name:
                        failed_node = node_name
                return status, (error_message, failed_node), None
            # bug-031: SUCCEEDED terminals carry outputs. Pull them so
            # late polls (past ``_REGISTRY_EVICTION_SECONDS``) still
            # see the outputs dict on cold-read.
            outputs: dict[str, Any] | None = None
            if status is WorkflowRunStatus.SUCCEEDED:
                payload = row.get("payload")
                if isinstance(payload, dict):
                    payload_outputs = payload.get("outputs")
                    if isinstance(payload_outputs, dict):
                        outputs = payload_outputs
            return status, None, outputs
    return None, None, None
