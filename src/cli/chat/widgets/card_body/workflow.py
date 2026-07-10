"""`WorkflowRenderer` — body renderer for the `workflow` card kind.

spec-013 §WorkflowRenderer. Registered in
[`KIND_TO_RENDERER`][cli.chat.widgets.card_body.KIND_TO_RENDERER]
so `Cards.open(kind="workflow")` mounts this renderer inside the
card's body slot. Consumes `kolsch.workflow.progress` UI events
(spec-012 emit format) and routes each executor event kind to
per-node state machines.

Composition (yielded from `compose`):

- [`WorkflowProgressHeader`][cli.chat.widgets.card_body.workflow.WorkflowProgressHeader]
  — `[N/M]  ▓▓░░░░░  Ns` progress bar + elapsed.
- [`WorkflowNodeList`][cli.chat.widgets.card_body.workflow.WorkflowNodeList]
  — container of
  [`WorkflowNodeRow`][cli.chat.widgets.workflow_node_row.WorkflowNodeRow]
  instances, one per node the executor spins up.
- [`WorkflowSummaryFooter`][cli.chat.widgets.card_body.workflow.WorkflowSummaryFooter]
  — empty by default; on a terminal `RUN_*` event renders
  `✓ done in Ns` / `✗ done in Ns`.

`apply_event(event_data)` decodes the wire kind (`kolsch.workflow.progress`)
and dispatches on the wrapped `WorkflowEventKind` value. `apply_snapshot`
is a documented no-op — workflow cards don't snapshot content the
way markdown cards do (spec-013 AC #14).

Sub-agent event routing is wired via
[`register_adapter`][cli.chat.widgets.card_body.workflow.WorkflowRenderer.register_adapter]:
when set, `NODE_STARTED` populates the adapter's
`_live_nodes_by_trace_and_invocation` map so incoming
`agent.message` / `tool.called` / `user.message` / `tool.result`
events with a matching `(trace_id, invocation_id)` route into the
node row (see `EventAdapter._handle_agent_message` et al.).
bug-033 rekeyed the map from `trace_id` alone to
`(trace_id, invocation_id)` because `trace_id` is run-wide —
without the composite key, sibling nodes clobbered each other's
registrations.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import Static

from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from schemas.wire_kinds import WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT
from workflows.events import WorkflowEventKind

if TYPE_CHECKING:
    from schemas.events import EventType as _EventType


_LOGGER = logging.getLogger(__name__)


_WIRE_KIND = "kolsch.workflow.progress"
"""The UI event envelope kind emitted by
[`WorkflowsImpl._drain_workflow_bridge`][cli.chat.services.workflows_impl.WorkflowsImpl._drain_workflow_bridge].
"""

_WIRE_KIND_AWAITING_APPROVAL = "kolsch.workflow.node.awaiting_approval"
"""spec-014 UI event kind — `WorkflowsImpl.emit_awaiting_approval`."""

_WIRE_KIND_APPROVAL_RESOLVED = "kolsch.workflow.node.approval_resolved"
"""spec-014 UI event kind — `WorkflowsImpl.emit_approval_resolved`."""

_WIRE_KIND_SUB_AGENT_EVENT = WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT
"""spec-020 UI event kind — `WorkflowsImpl._emit_sub_agent_ui_event`.
Envelope carries `{run_id, node_name, invocation_id, trace_id,
event_type, event_id, payload}`; the renderer reconstructs a
lightweight event object and hands it to
`WorkflowNodeRow.apply_sub_agent_event`."""


_TERMINAL_RUN_KINDS = frozenset({
    WorkflowEventKind.RUN_COMPLETED.value,
    WorkflowEventKind.RUN_FAILED.value,
    WorkflowEventKind.RUN_ABORTED.value,
    WorkflowEventKind.RUN_TIMED_OUT.value,
})
"""The four terminal run kinds the footer summarises."""


@dataclass(frozen=True)
class _ReconstructedSubAgentEvent:
    """spec-020 event stub for `WorkflowNodeRow.apply_sub_agent_event`.

    The row's mount hook (`_mount_sub_agent_widget`) only reads three
    attributes off the passed event: `type` (compared against
    `EventType` members), `data` (a dict), and `created_at` (a
    datetime, used only by `UserChatbox`). We reconstruct a lightweight
    duck-typed object rather than paying the cost of building a full
    pydantic `Event` per session-scoped emit — the wire envelope
    doesn't carry the `session_id` / `thread_id` / `actor` /
    `event_id` fields the pydantic model requires, and none of them
    are load-bearing at the widget layer.

    `trace_id` is carried for symmetry with the real `Event` shape;
    it's not currently read by any consumer downstream of this
    dispatch, but future spec-021 (backfill) may want it.
    """

    type: "_EventType"
    data: dict[str, Any]
    created_at: datetime
    trace_id: str


class WorkflowProgressHeader(Static):
    """Progress line: `[completed/total]  ▓▓░░░░░  Ns`.

    Updated by `WorkflowRenderer._update_header` on every event; the
    glyph bar is an 8-cell fill ratio (not pixel-precise; a coarse
    visual cue is enough for the terminal). Empty totals render as
    `[0/0]  ░░░░░░░░  0s`.

    spec-017 adds an optional ``parent_ref``: when set, the header
    prepends a ``↳ from <parent-ref>`` hint line above the progress
    bar so a nested workflow's card visually threads its causal
    parent without introducing a visual-hierarchy nest (r4
    flat-cards invariant). ``parent_ref=None`` (top-level or peer
    from an outer POV) restores the single-line render.
    """

    _BAR_WIDTH = 8
    """Fixed number of glyph cells in the progress bar."""

    def __init__(
        self,
        *,
        id: str | None = None,
        parent_ref: str | None = None,
    ) -> None:
        self._run_id: str | None = None
        self._completed = 0
        self._total = 0
        self._elapsed_seconds: float = 0.0
        # spec-017: cache the parent's ref string so the render
        # can prepend `↳ from <parent-ref>` on its own line above
        # the progress bar. `None` (the top-level case) restores
        # the single-line render.
        self._parent_ref: str | None = parent_ref
        super().__init__(self._compute_text(), id=id)

    def set_run_id(self, run_id: str | None) -> None:
        """Store the run id anchor; re-render."""
        self._run_id = run_id
        self._refresh_progress()

    def set_parent_ref(self, parent_ref: str | None) -> None:
        """Set / clear the ``↳ from <parent-ref>`` header hint.

        spec-017 lets the adapter update the hint after construction
        (e.g., when the parent record's ``ref`` isn't known at
        widget-mount time and lands via a follow-up event). Idempotent
        — passing the same value is a no-op refresh.
        """
        self._parent_ref = parent_ref
        self._refresh_progress()

    def update_progress(
        self,
        completed: int,
        total: int,
        elapsed: float,
    ) -> None:
        """Refresh the counters + elapsed; re-render."""
        self._completed = completed
        self._total = total
        self._elapsed_seconds = elapsed
        self._refresh_progress()

    def _compute_text(self) -> str:
        """Compose the current progress-line text; no widget mutation.

        spec-017: when ``parent_ref`` is set, prepend
        ``↳ from <parent-ref>`` on its own line above the progress
        bar. A ``None`` parent_ref (top-level or peer from an outer
        POV) restores the single-line render.
        """
        total = max(self._total, 0)
        completed = max(0, min(self._completed, total)) if total else 0
        if total > 0:
            filled = int(round(self._BAR_WIDTH * completed / total))
        else:
            filled = 0
        bar = "▓" * filled + "░" * (self._BAR_WIDTH - filled)
        progress_line = (
            f"[{completed}/{total}]  {bar}  {int(self._elapsed_seconds)}s"
        )
        if self._parent_ref:
            return f"↳ from {self._parent_ref}\n{progress_line}"
        return progress_line

    def _refresh_progress(self) -> None:
        """Redraw the progress line from the current counters."""
        self.update(self._compute_text())


class WorkflowNodeList(Vertical):
    """Container for `WorkflowNodeRow` instances."""

    def add_node(self, node_row: WorkflowNodeRow) -> None:
        """Mount `node_row` at the end of the list.

        Best-effort — headless tests exercise the renderer without a
        live mount tree, so a mount failure is swallowed. The row is
        still tracked in `WorkflowRenderer._nodes` so state
        transitions land without a DOM handle.
        """
        with contextlib.suppress(Exception):
            self.mount(node_row)


class WorkflowSummaryFooter(Static):
    """Post-terminal one-liner (`✓ done in Ns` / `✗ done in Ns`).

    Empty by default; `WorkflowRenderer._on_terminal` populates on
    the first terminal `RUN_*` event.
    """


class WorkflowRenderer(Vertical):
    """`workflow` card body renderer.

    Wraps a progress header + node list + summary footer. Every
    `kolsch.workflow.progress` UI event flows through `apply_event`
    which decodes the wrapped `WorkflowEventKind` and dispatches to
    the appropriate sub-widget update.

    spec-016 adds ``compact_mode`` — a reactive flag that hides the
    node list + footer and swaps in a one-line summary
    (``▸ workflow · <ref>  N/M · Ns``). The
    [`WorkflowLayoutManager`][cli.chat.services.workflow_layout.WorkflowLayoutManager]
    flips this when more than two workflows are in-flight so the
    transcript stays legible under load. Focused cards remain full-
    render; terminal-group cards are always compact.
    """

    DEFAULT_CSS = """
    WorkflowRenderer {
        height: auto;
        background: transparent;
    }
    WorkflowRenderer WorkflowProgressHeader { height: auto; }
    WorkflowRenderer WorkflowNodeList { height: auto; }
    WorkflowRenderer WorkflowSummaryFooter { height: auto; }
    WorkflowRenderer.-compact WorkflowNodeList { display: none; }
    WorkflowRenderer.-compact WorkflowSummaryFooter { display: none; }
    """

    compact_mode: reactive[bool] = reactive(False, init=False)
    """Compact-mode toggle (spec-016).

    ``True`` hides the node list + footer and shows a single-line
    summary via ``_compact_line``. ``False`` restores the full render.
    The
    [`WorkflowLayoutManager`][cli.chat.services.workflow_layout.WorkflowLayoutManager]
    writes this from its 250ms reorder tick.
    """

    def __init__(
        self,
        *,
        id: str | None = None,
        parent_ref: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._node_rows: dict[str, WorkflowNodeRow] = {}
        self._run_id: str | None = None
        self._workflow_ref: str | None = None
        self._start_time: datetime | None = None
        self._last_ts: datetime | None = None
        # spec-017: cache the parent ref so `set_parent_ref` calls
        # after construction (adapter learns the parent after mount)
        # keep the header in sync.
        self._parent_ref: str | None = parent_ref
        # bug-031: terminal flag — flipped by ``_on_terminal`` so the
        # 1Hz tick timer stops advancing elapsed once the summary
        # footer has landed. Wall-clock elapsed prior to that keeps
        # the header ticking even when the executor bridge goes
        # quiet mid-run (session ses_af5980ab: 90.4s dead zone on
        # deduper node without a widget tick).
        self._is_terminal: bool = False
        # bug-031: 1Hz elapsed tick timer handle. Installed in
        # ``on_mount``; stopped in ``_on_terminal``. Established
        # convention: ``turn_status.py::_start_timers`` uses
        # ``set_interval(1.0, ...)``.
        self._tick_handle: Any | None = None
        # spec-013 / bug-033: adapter reference for
        # `_live_nodes_by_trace_and_invocation` registration.
        # `register_adapter` (called by the mount helper on the
        # ChatApp side) sets this. When `None`, node registration
        # is a no-op — headless tests exercise the renderer without
        # a live adapter and rely on the state transitions only.
        self._adapter: Any | None = None
        # Body sub-widgets — instantiated in `__init__` so headless
        # tests can access them BEFORE `compose` runs (mypy prefers
        # attribute-init in `__init__`). `compose` yields the same
        # instances into the DOM.
        self._header = WorkflowProgressHeader(
            id=(f"{id}-header" if id else None),
            parent_ref=parent_ref,
        )
        self._node_list = WorkflowNodeList(id=(f"{id}-nodes" if id else None))
        self._footer = WorkflowSummaryFooter(id=(f"{id}-footer" if id else None))
        # spec-016: compact-mode summary line (hidden by default).
        # Uses a distinct id so tests can query it. The line is empty
        # until the watcher populates it on a compact flip.
        self._compact_line = Static(
            "", id=(f"{id}-compact" if id else None),
        )
        self._compact_line.display = False

    def compose(self) -> ComposeResult:
        """Yield header + node list + footer + compact summary."""
        yield self._header
        yield self._node_list
        yield self._footer
        yield self._compact_line

    def on_mount(self) -> None:
        """bug-031: install the 1Hz elapsed-tick timer.

        Session ``ses_af5980ab`` had a 90.4s dead zone on the
        ``deduper`` node and a 65s dead zone on ``synthesize`` because
        the pre-bug-031 renderer only refreshed the header on an
        incoming ``kolsch.workflow.progress`` UI event. Between
        executor events the elapsed counter froze. Convention
        established at ``turn_status.py::_start_timers`` (line 197,
        ``set_interval(1.0, self._tick_elapsed)``) — apply the same
        pattern here. ``_tick_elapsed`` early-outs on
        ``self._is_terminal`` so the terminal footer stays quiescent.
        """
        self._tick_handle = self.set_interval(1.0, self._tick_elapsed)

    def _tick_elapsed(self) -> None:
        """bug-031: re-render the header from wall-clock elapsed.

        Runs on the 1Hz interval installed in ``on_mount``. Skips
        while ``_start_time`` is unset (renderer hasn't seen
        RUN_STARTED yet) or after ``_is_terminal`` (summary footer
        has landed and should stop moving).
        """
        if self._start_time is None or self._is_terminal:
            return
        self._update_header()

    # ------------------------------------------------------------------
    # Public seams
    # ------------------------------------------------------------------

    def set_parent_ref(self, parent_ref: str | None) -> None:
        """Update the ``↳ from <parent-ref>`` header hint after mount.

        spec-017: the adapter learns a run's ``parent_run_id`` on the
        first ``kolsch.workflow.progress`` fan-out (workflow-scoped
        prompts have a different path), so it may need to refresh the
        hint after the renderer already mounted. Idempotent —
        passing the same value re-renders the header without other
        side effects.
        """
        self._parent_ref = parent_ref
        self._header.set_parent_ref(parent_ref)

    def register_adapter(self, adapter: Any) -> None:
        """Wire an `EventAdapter` so node starts populate its trace map.

        Called from the ChatApp mount hook once the adapter is
        available. When set, `NODE_STARTED` inserts the row into
        `adapter._live_nodes_by_trace_and_invocation[(trace_id,
        invocation_id)]` and node terminals pop it — this is the
        seam the adapter uses to route sub-agent events into the
        right node.

        bug-033: the map key is a `(trace_id, invocation_id)` tuple
        because `trace_id` alone is run-wide and sibling nodes
        would clobber each other's registrations.
        """
        self._adapter = adapter

    def apply_event(self, event_data: Any) -> None:
        """Dispatch a `kolsch.workflow.progress` payload to sub-widgets.

        Also handles the spec-014 approval-lifecycle kinds
        (`kolsch.workflow.node.awaiting_approval` /
        `.approval_resolved`) which transition the matching
        `WorkflowNodeRow` between `paused` and `running`.

        Non-workflow kinds (any other card wire event) are silently
        ignored — the CardWidget's own dispatcher handles them.
        Malformed payloads are silently dropped so a replayed
        malformed row can't crash the renderer.
        """
        kind = getattr(event_data, "kind", None)
        if kind is None and isinstance(event_data, dict):
            kind = event_data.get("kind")
        payload = getattr(event_data, "payload", None)
        if payload is None and isinstance(event_data, dict):
            payload = event_data.get("payload")
        if not isinstance(payload, dict):
            return

        # spec-014: approval-lifecycle kinds transition a matching
        # `WorkflowNodeRow` without touching header / footer / counts.
        if kind == _WIRE_KIND_AWAITING_APPROVAL:
            self._on_awaiting_approval(payload)
            return
        if kind == _WIRE_KIND_APPROVAL_RESOLVED:
            self._on_approval_resolved(payload)
            return
        # spec-020: sub-agent event forwarder. Routes into the
        # matching `WorkflowNodeRow` via bug-033's
        # `(trace_id, invocation_id)` composite map.
        if kind == _WIRE_KIND_SUB_AGENT_EVENT:
            self._on_sub_agent_event(payload)
            return
        if kind != _WIRE_KIND:
            return

        workflow_event_kind = payload.get("kind")
        if not isinstance(workflow_event_kind, str):
            return

        if workflow_event_kind == WorkflowEventKind.RUN_STARTED.value:
            self._on_run_started(payload)
        elif workflow_event_kind == WorkflowEventKind.NODE_STARTED.value:
            self._on_node_started(payload)
        elif workflow_event_kind == WorkflowEventKind.NODE_COMPLETED.value:
            self._on_node_completed(payload)
        elif workflow_event_kind in (
            WorkflowEventKind.NODE_FAILED.value,
            WorkflowEventKind.NODE_TIMED_OUT.value,
        ):
            self._on_node_failed(payload)
        elif workflow_event_kind == WorkflowEventKind.NODE_PRUNED.value:
            self._on_node_pruned(payload)
        elif workflow_event_kind in _TERMINAL_RUN_KINDS:
            self._on_terminal(workflow_event_kind, payload)
        # Non-terminal / non-dispatched kinds are ignored — the
        # header still refreshes below so elapsed advances.

        self._track_ts(payload)
        self._update_header()

    def apply_snapshot(self, content: str) -> None:
        """No-op — workflow cards don't snapshot content (AC #14).

        Documented in spec-013 §AC 14: markdown cards use snapshots
        to replace the whole document; workflow cards derive their
        display from the streaming executor event log and have no
        equivalent snapshot payload. `content` is accepted for
        Protocol conformance and dropped without side effects.
        """
        _ = content

    # ------------------------------------------------------------------
    # Per-kind handlers
    # ------------------------------------------------------------------

    def _on_run_started(self, payload: dict[str, Any]) -> None:
        """Stash `run_id` + `start_time`; header refreshes below."""
        run_id = payload.get("run_id")
        if isinstance(run_id, str):
            self._run_id = run_id
            self._header.set_run_id(run_id)
        # spec-016: cache the workflow ref for the compact-mode summary.
        workflow_ref = payload.get("workflow_ref")
        if isinstance(workflow_ref, str):
            self._workflow_ref = workflow_ref
        ts = self._parse_ts(payload.get("ts"))
        if ts is not None:
            self._start_time = ts
            self._last_ts = ts

    def _on_node_started(self, payload: dict[str, Any]) -> None:
        """Mount a `WorkflowNodeRow`; register `(trace_id, invocation_id)`.

        bug-033: extracts `invocation_id` from the payload alongside
        `trace_id` so the adapter's routing map can key on the
        per-node composite. `invocation_id` is hoisted at the
        envelope top level by
        `WorkflowsImpl._drain_workflow_bridge`; a missing value
        (older payload shape) falls back to the trace-only sentinel
        with a WARNING log inside `_register_node_with_adapter`.
        """
        node_name = payload.get("node_name")
        if not isinstance(node_name, str) or not node_name:
            return
        trace_id = payload.get("trace_id")
        trace_id_val = trace_id if isinstance(trace_id, str) else None
        invocation_id = payload.get("invocation_id")
        invocation_id_val = (
            invocation_id if isinstance(invocation_id, str) else None
        )
        # bug-036a: forward the node type (e.g. ``"kolsch/nodes/agent"``)
        # so the row's ``set_output`` gate can suppress the JSON
        # preview when an agent response already rendered. Executor
        # emits ``type`` nested under ``payload.payload`` (see the
        # NODE_STARTED envelope shape); fall back to a top-level
        # ``payload.type`` for older payload shapes just in case.
        node_type_val: str | None = None
        inner = payload.get("payload")
        if isinstance(inner, dict):
            candidate = inner.get("type")
            if isinstance(candidate, str):
                node_type_val = candidate
        if node_type_val is None:
            candidate = payload.get("type")
            if isinstance(candidate, str):
                node_type_val = candidate
        if node_name in self._node_rows:
            # Idempotent — a replay of the same NODE_STARTED shouldn't
            # mount a second row.
            return
        row = WorkflowNodeRow(
            node_name=node_name,
            trace_id=trace_id_val,
            node_type=node_type_val,
        )
        self._node_rows[node_name] = row
        self._node_list.add_node(row)
        self._register_node_with_adapter(
            row, trace_id_val, invocation_id_val,
        )

    def _on_node_completed(self, payload: dict[str, Any]) -> None:
        """Transition the row to `completed`; inline any summary.

        bug-032: also extract `payload.output` (the real emitter
        field — bash: `{stdout, stderr, exit_code}`; agent: bound
        outputs) and forward it to
        [`WorkflowNodeRow.set_output`][cli.chat.widgets.workflow_node_row.WorkflowNodeRow.set_output]
        so the expanded body renders the completed-node data instead
        of staying empty.
        """
        node_name = payload.get("node_name")
        if not isinstance(node_name, str):
            return
        row = self._node_rows.get(node_name)
        if row is None:
            return
        details = None
        output: Any = None
        inner = payload.get("payload")
        if isinstance(inner, dict):
            candidate = inner.get("summary")
            if isinstance(candidate, str):
                details = candidate
            # bug-032: `output` is the real field the executor emits
            # at ``src/workflows/executor.py:1482-1536``. Pre-bug-032
            # the renderer read only ``summary`` (never emitted) and
            # dropped ``output`` on the floor.
            output = inner.get("output")
        # Fallback: top-level `summary` (in case the emitter hoists).
        if details is None:
            candidate = payload.get("summary")
            if isinstance(candidate, str):
                details = candidate
        row.set_state("completed", details=details)
        # bug-032: forward the preview AFTER `set_state` so the row
        # is already in its terminal palette when the body mounts.
        # `set_output` is a no-op for `None` / empty containers so we
        # can call it unconditionally.
        row.set_output(output)
        trace_id = payload.get("trace_id")
        invocation_id = payload.get("invocation_id")
        self._unregister_node_from_adapter(
            trace_id if isinstance(trace_id, str) else None,
            invocation_id if isinstance(invocation_id, str) else None,
        )

    def _on_node_failed(self, payload: dict[str, Any]) -> None:
        """Transition the row to `failed`; inline the error preview."""
        node_name = payload.get("node_name")
        if not isinstance(node_name, str):
            return
        row = self._node_rows.get(node_name)
        if row is None:
            return
        error_preview: str | None = None
        inner = payload.get("payload")
        if isinstance(inner, dict):
            for key in ("error_message", "message", "error"):
                candidate = inner.get(key)
                if isinstance(candidate, str):
                    error_preview = candidate
                    break
        if error_preview is None:
            for key in ("error_message", "message", "error"):
                candidate = payload.get(key)
                if isinstance(candidate, str):
                    error_preview = candidate
                    break
        row.set_state("failed", error_preview=error_preview)
        trace_id = payload.get("trace_id")
        invocation_id = payload.get("invocation_id")
        self._unregister_node_from_adapter(
            trace_id if isinstance(trace_id, str) else None,
            invocation_id if isinstance(invocation_id, str) else None,
        )

    def _on_node_pruned(self, payload: dict[str, Any]) -> None:
        """Transition the row to `pruned` (glyph `─`)."""
        node_name = payload.get("node_name")
        if not isinstance(node_name, str):
            return
        row = self._node_rows.get(node_name)
        if row is None:
            return
        row.set_state("pruned")
        trace_id = payload.get("trace_id")
        invocation_id = payload.get("invocation_id")
        self._unregister_node_from_adapter(
            trace_id if isinstance(trace_id, str) else None,
            invocation_id if isinstance(invocation_id, str) else None,
        )

    def _on_awaiting_approval(self, payload: dict[str, Any]) -> None:
        """Transition the matching row to `paused` (glyph `⚠`).

        spec-014 §Renderer wiring. `run_id` scoping is enforced by
        `EventAdapter._handle_workflow_node_awaiting_approval` before
        the dispatch reaches this renderer, so we route purely on
        `node_name` here.
        """
        node_name = payload.get("node_name")
        if not isinstance(node_name, str) or not node_name:
            return
        row = self._node_rows.get(node_name)
        if row is None:
            return
        row.set_state("paused")

    def _on_sub_agent_event(self, payload: dict[str, Any]) -> None:
        """Route a `kolsch.workflow.node.sub_agent_event` to a row.

        spec-020. Reads `trace_id` + `invocation_id` off the envelope,
        looks up the target `WorkflowNodeRow` in the adapter's
        `_live_nodes_by_trace_and_invocation[(trace_id, invocation_id)]`
        map (bug-033 composite key), reconstructs a lightweight event
        object with the surface `WorkflowNodeRow.apply_sub_agent_event`
        + `_mount_sub_agent_widget` consume (`type`, `data`,
        `created_at`, `trace_id`), and dispatches.

        Malformed / stale payloads (missing trace_id, missing
        invocation_id, invalid event_type, no matching row) are
        silently dropped — the whole spec-020 stream is best-effort,
        matching the existing `_route_to_workflow_node` shape on
        `EventAdapter`.
        """
        trace_id = payload.get("trace_id")
        invocation_id = payload.get("invocation_id")
        event_type_raw = payload.get("event_type")
        if (
            not isinstance(trace_id, str)
            or not isinstance(invocation_id, str)
            or not isinstance(event_type_raw, str)
            or not trace_id
        ):
            return
        # Resolve the target row. Prefer the adapter's routing map
        # (populated on NODE_STARTED via `_register_node_with_adapter`);
        # fall back to the local `_node_rows` when the adapter isn't
        # wired (headless tests exercise the renderer directly).
        row: WorkflowNodeRow | None = None
        if self._adapter is not None:
            live_map = getattr(
                self._adapter, "_live_nodes_by_trace_and_invocation", None,
            )
            if isinstance(live_map, dict):
                candidate = live_map.get((trace_id, invocation_id))
                if isinstance(candidate, WorkflowNodeRow):
                    row = candidate
        if row is None:
            # Adapter-less fallback: the node name isn't on the
            # envelope, but bug-033's routing map keys on
            # `(trace_id, invocation_id)` — scan the local rows and
            # match by invocation. In practice this only fires from
            # the adapter-less unit test harness.
            node_name = payload.get("node_name")
            if isinstance(node_name, str):
                row = self._node_rows.get(node_name)
        if row is None:
            return
        # Reconstruct a minimal event object with the surface the
        # row's `_mount_sub_agent_widget` reads: `type` (compared to
        # `EventType.*`), `data` (dict), `created_at`. We DON'T
        # rebuild a full pydantic `Event` — the envelope-level
        # `event_id` / `session_id` / `thread_id` / `actor` are not
        # consumed by any downstream widget, and running Pydantic
        # validation on every sub-agent event would tax the drain
        # for zero UI benefit. `SimpleNamespace` matches the duck-
        # typed surface exactly.
        try:
            from schemas.events import EventType  # noqa: PLC0415
            event_type = EventType(event_type_raw)
        except (ValueError, ImportError):
            # Unknown event type — nothing to render.
            return
        raw_data = payload.get("payload")
        data = dict(raw_data) if isinstance(raw_data, dict) else {}
        reconstructed = _ReconstructedSubAgentEvent(
            type=event_type,
            data=data,
            created_at=datetime.now(tz=UTC),
            trace_id=trace_id,
        )
        with contextlib.suppress(Exception):
            row.apply_sub_agent_event(reconstructed)

    def _on_approval_resolved(self, payload: dict[str, Any]) -> None:
        """Transition the matching row back to `running` (glyph `▸`).

        Fires on `kolsch.workflow.node.approval_resolved` regardless
        of the payload's `resolution` (`approved` / `denied` /
        `cancelled`) — the executor's follow-up NODE_* events drive
        any subsequent terminal transition.
        """
        node_name = payload.get("node_name")
        if not isinstance(node_name, str) or not node_name:
            return
        row = self._node_rows.get(node_name)
        if row is None:
            return
        row.set_state("running")

    def _on_terminal(self, kind: str, payload: dict[str, Any]) -> None:
        """Populate the footer with `✓ done in Ns` / `✗ done in Ns`.

        bug-031: flip ``_is_terminal`` so the ``_tick_elapsed`` timer
        stops refreshing the header — the footer is now the frozen
        summary. Also stop the tick handle to avoid unnecessary
        wake-ups (best-effort — Textual's ``Timer.stop`` swallows
        the double-stop shutdown case).
        """
        success = kind == WorkflowEventKind.RUN_COMPLETED.value
        elapsed = self._elapsed_seconds(payload)
        prefix = "✓" if success else "✗"
        self._footer.update(f"{prefix} done in {int(elapsed)}s")
        self._is_terminal = True
        if self._tick_handle is not None:
            with contextlib.suppress(Exception):
                self._tick_handle.stop()
            self._tick_handle = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _track_ts(self, payload: dict[str, Any]) -> None:
        """Advance `_last_ts` from the payload's ISO timestamp."""
        ts = self._parse_ts(payload.get("ts"))
        if ts is not None:
            self._last_ts = ts
            if self._start_time is None:
                self._start_time = ts

    def _elapsed_seconds(self, payload: dict[str, Any]) -> float:
        """Prefer payload's explicit `elapsed_seconds`; else derive from ts."""
        inner = payload.get("payload")
        if isinstance(inner, dict):
            for key in ("elapsed_seconds", "duration_seconds"):
                candidate = inner.get(key)
                if isinstance(candidate, (int, float)):
                    return float(candidate)
        candidate_top = payload.get("elapsed_seconds")
        if isinstance(candidate_top, (int, float)):
            return float(candidate_top)
        if self._start_time is None:
            return 0.0
        end_ts = self._parse_ts(payload.get("ts")) or self._last_ts
        if end_ts is None:
            return 0.0
        return max(0.0, (end_ts - self._start_time).total_seconds())

    @staticmethod
    def _parse_ts(raw: Any) -> datetime | None:
        """Parse an ISO-8601 string / datetime into a `datetime`.

        Returns `None` for anything else — the caller falls back to
        the renderer's own bookkeeping.
        """
        if isinstance(raw, datetime):
            return raw
        if isinstance(raw, str):
            with contextlib.suppress(ValueError):
                return datetime.fromisoformat(raw)
        return None

    def _update_header(self) -> None:
        """Refresh the header counters from the current node state."""
        total = len(self._node_rows)
        completed = sum(
            1
            for row in self._node_rows.values()
            if row.state in ("completed", "failed", "pruned")
        )
        elapsed = self._elapsed_now()
        self._header.update_progress(completed, total, elapsed)
        # spec-016: keep the compact-summary line in sync so a compact
        # card's single-line stays current under streaming updates.
        if self.compact_mode:
            self._compact_line.update(self._render_compact_summary())

    # ------------------------------------------------------------------
    # spec-016: compact mode
    # ------------------------------------------------------------------

    def watch_compact_mode(self, old: bool, new: bool) -> None:
        """React to a ``compact_mode`` flip — hide/show node list + footer.

        Also toggles the ``.-compact`` CSS class on the renderer so
        stylesheet rules can key off the mode without a reactive
        subscription.
        """
        _ = old
        if new:
            self._node_list.display = False
            self._footer.display = False
            self._compact_line.display = True
            self._compact_line.update(self._render_compact_summary())
            self.add_class("-compact")
        else:
            self._node_list.display = True
            self._footer.display = True
            self._compact_line.display = False
            self.remove_class("-compact")

    def _render_compact_summary(self) -> str:
        """Return the ``▸ workflow · <ref>  N/M · Ns`` one-liner.

        Ref falls back to the run id (or ``?``) when unset — replay
        may leave the renderer with no workflow_ref anchor.
        """
        completed = self._header._completed
        total = self._header._total
        elapsed = int(self._header._elapsed_seconds)
        ref = self._workflow_ref or self._run_id or "?"
        return f"▸ workflow · {ref}  {completed}/{total} · {elapsed}s"

    def _elapsed_now(self) -> float:
        """Return seconds since `_start_time`.

        bug-031: prefer WALL-CLOCK once ``_start_time`` is set. The
        pre-bug-031 code anchored on ``self._last_ts or
        datetime.now(UTC)`` — but ``_last_ts`` advances with each
        emitted event, so between events the anchor froze and the
        header displayed the same integer indefinitely (session
        ses_af5980ab: 90.4s ``deduper`` dead zone). Wall-clock
        matches the 1Hz ``_tick_elapsed`` cadence + the impl-side
        ``format_live_workflow_strip`` wall-clock read
        (``workflow_layout.py:150-158``).
        """
        if self._start_time is None:
            return 0.0
        anchor = datetime.now(tz=UTC)
        with contextlib.suppress(TypeError):
            return max(0.0, (anchor - self._start_time).total_seconds())
        return 0.0

    # ------------------------------------------------------------------
    # Adapter registration hooks (spec-013 §EventAdapter trace_id routing)
    # ------------------------------------------------------------------

    def _register_node_with_adapter(
        self,
        row: WorkflowNodeRow,
        trace_id: str | None,
        invocation_id: str | None,
    ) -> None:
        """Store `row` in the adapter's routing map.

        bug-033: keyed on `(trace_id, invocation_id)` — the
        `trace_id`-only key clobbered sibling nodes because
        `trace_id` is run-wide. Missing `invocation_id` (older
        payload shape or replay) falls back to the empty-string
        sentinel and logs a WARNING; the fallback preserves
        pre-bug-033 behaviour (single map entry per trace) so
        replay of old sessions doesn't crash.
        """
        if self._adapter is None or trace_id is None:
            return
        live_map = getattr(
            self._adapter, "_live_nodes_by_trace_and_invocation", None,
        )
        if not isinstance(live_map, dict):
            return
        key_invocation = invocation_id
        if key_invocation is None:
            node_name = getattr(row, "_node_name", "<unknown>")
            _LOGGER.warning(
                "bug-033: NODE_STARTED payload for node %s "
                "(trace_id=%s) missing invocation_id; falling "
                "back to trace-only routing key. Sibling nodes "
                "may collide.",
                node_name,
                trace_id,
            )
            key_invocation = ""
        live_map[(trace_id, key_invocation)] = row

    def _unregister_node_from_adapter(
        self,
        trace_id: str | None,
        invocation_id: str | None,
    ) -> None:
        """Pop the `(trace_id, invocation_id)` key from the routing map.

        bug-033: pops the exact composite key so terminal events
        can't evict a still-live sibling node. Missing
        `invocation_id` falls back to the sentinel key that
        `_register_node_with_adapter` wrote in the same-payload-
        shape case (both writer and unwriter use ``""``).
        """
        if self._adapter is None or trace_id is None:
            return
        live_map = getattr(
            self._adapter, "_live_nodes_by_trace_and_invocation", None,
        )
        if not isinstance(live_map, dict):
            return
        key_invocation = invocation_id if invocation_id is not None else ""
        live_map.pop((trace_id, key_invocation), None)


__all__ = [
    "WorkflowNodeList",
    "WorkflowProgressHeader",
    "WorkflowRenderer",
    "WorkflowSummaryFooter",
]
