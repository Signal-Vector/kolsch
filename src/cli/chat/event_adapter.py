"""Event → widget dispatcher for the Textual chat surface.

Consumes events from
[`AgentEngine.execute_stream`][engine.engine.AgentEngine.execute_stream]
and mounts / updates widgets in the transcript. Preserves the
"renderer is a pure event consumer" invariant: dispatch is a class-
level table over ``EventType``; one ``_handle_<name>`` method per
event type; no widget-specific state carried in the adapter.

The single approval carve-out lives in ``cli/chat/approval.py`` —
the adapter has no idea approvals exist.

Text extraction, tool-arg formatting, and the fenced-code-block
heuristic are ported verbatim from ``src/cli/_renderer.py`` and
kept as module-level helpers so per-adapter tests can exercise
them without an ``App``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import OrderedDict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import CardId, EventId
from schemas.wire_kinds import (
    WIRE_CARD_APPENDED,
    WIRE_CARD_CLOSED,
    WIRE_CARD_NOTICE,
    WIRE_CARD_OPENED,
    WIRE_CARD_SET,
    WIRE_CARD_SET_PROGRESS,
    WIRE_CARD_SNAPSHOT,
    WIRE_CARD_STATUS,
    WIRE_PROMPT_ANSWERED,
    WIRE_PROMPT_CANCELLED,
    WIRE_PROMPT_REQUESTED,
    WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED,
    WIRE_WORKFLOW_NODE_AWAITING_APPROVAL,
    WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT,
    WIRE_WORKFLOW_PROGRESS,
)

if TYPE_CHECKING:
    from cli.chat.app import ChatApp
    from cli.chat.services.prompts_impl import PromptsImpl
    from cli.chat.services.workflows_impl import WorkflowsImpl
    from schemas import SessionMeta
    from schemas.identifiers import SessionId, ThreadId
    from store.session_store import SessionStore


_PROMPT_LRU_MAXSIZE = 1000
"""spec-004 AC #7: bounded LRU maxsize for the prompt-idempotence set.

At 1000 unique `prompt_id`s the LRU is best-effort — the 1001st
insert evicts the earliest. Legitimate chat sessions never approach
this bound within a single boot; the cap is a memory guard, not a
correctness invariant.
"""


class _BoundedLRUSet:
    """Insertion-ordered set with an upper size bound.

    On `add`, evicts the oldest entry once `len` exceeds `maxsize`.
    Membership tests are O(1). No stdlib LRU set exists so the
    adapter carries this tiny helper; the invariant matches spec-004
    AC #7.
    """

    __slots__ = ("_maxsize", "_seen")

    def __init__(self, *, maxsize: int) -> None:
        self._maxsize = maxsize
        self._seen: OrderedDict[str, None] = OrderedDict()

    def __contains__(self, key: str) -> bool:
        return key in self._seen

    def __len__(self) -> int:
        return len(self._seen)

    def add(self, key: str) -> None:
        """Insert `key`, evicting the oldest entry if the bound is hit.

        Idempotent: re-adding a present key refreshes its position
        so it stays fresh in the LRU.
        """
        if key in self._seen:
            self._seen.move_to_end(key)
            return
        self._seen[key] = None
        while len(self._seen) > self._maxsize:
            self._seen.popitem(last=False)


_LOGGER = logging.getLogger(__name__)


def _response_to_resolution(response: str) -> str:
    """Map a `PromptsImpl` response string to a `resolution` verb.

    spec-014: `emit_approval_resolved` payload's `resolution` is one
    of `"approved"` / `"denied"` / `"cancelled"`. `PromptsImpl`
    surfaces the raw key (`y` / `a` / `n` / `d`) via
    `.answered.response`; `.cancelled` reaches this via the sync
    handler with `response="cancelled"` synthesised.
    """
    if response == "cancelled":
        return "cancelled"
    if response in ("y", "a"):
        return "approved"
    return "denied"


def _replay_outcome_from_response(
    response: str,
    *,
    has_rule: bool,
) -> tuple[str, str]:
    """Map a historical `.answered` response key to a terminal outcome.

    bug-056. Mirrors the interactive path's `_resolve` footer choice
    at `approval_block.py:283-390` — the resumed row must read exactly
    the same footer/class the user saw when they originally answered.

    - ``y`` → ``("allowed", "✓ Allowed once")``
    - ``a`` + rule dict → ``("allowed", "✓ Rule saved (allow)")``
    - ``a`` without rule dict → ``("allowed", "✓ Allowed once")`` (a
      pre-bug-025 answered payload that dropped the rule; the fallback
      matches `_translate_response`'s allow-once collapse).
    - ``n`` → ``("denied", "✗ Denied once")``
    - ``d`` + rule dict → ``("denied", "✗ Rule saved (deny)")``
    - ``d`` without rule dict → ``("denied", "✗ Denied once")``
    - Anything else (unknown response) → denied — matches
      `_translate_response`'s catch-all deny.
    """
    if response == "y":
        return ("allowed", "✓ Allowed once")
    if response == "a":
        if has_rule:
            return ("allowed", "✓ Rule saved (allow)")
        return ("allowed", "✓ Allowed once")
    if response == "d":
        if has_rule:
            return ("denied", "✗ Rule saved (deny)")
        return ("denied", "✗ Denied once")
    return ("denied", "✗ Denied once")


def _log_task_exception(task: asyncio.Task[Any]) -> None:
    """Done-callback that surfaces a failed fire-and-forget task.

    Prevents a silent-swallow on the spec-014 emit path: if
    `emit_approval_resolved` raises, the logger records it.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _LOGGER.exception(
            "event_adapter: fire-and-forget task raised",
            exc_info=exc,
        )


def _is_card_mounting_tool(tool_name: str) -> bool:
    """True when the tool opens its own card, making the transcript block redundant.

    bug-036a: catches ``run_workflow`` and bug-028's per-workflow
    virtuals (``run_workflow_<bundle>_<name>``). Both open a
    ``WorkflowCard`` — the transcript-root ToolCallBlock underneath
    is redundant with the card body.
    """
    return tool_name == "run_workflow" or tool_name.startswith("run_workflow_")


# ---------------------------------------------------------------------------
# spec-006: CardMountState Protocol + in-memory stub
# ---------------------------------------------------------------------------

_CARD_APPEND_LRU_MAXSIZE = 1000
"""Per-card LRU bound for applied append `event_id`s. spec-006 AC #11."""

_CARD_SNAPSHOT_LRU_MAXSIZE = 100
"""Per-card LRU bound for applied snapshot `event_id`s. spec-006 AC #11."""

_CARD_NOTICE_LRU_MAXSIZE = 200
"""Bound for applied notice ids across the adapter. spec-006 AC #11."""


class CardMountState(Protocol):
    """spec-006 r-review r1 seam.

    Every `EventAdapter._handle_card_*` handler reads/writes state via
    this Protocol only — no direct field access on the concrete impl.
    spec-006 ships a stub impl (`_CardMountStub`) behind the Protocol;
    spec-007 fills the real widget-backed impl (`CardWidget`) without
    rewriting handler code.

    LOCKED contract: handlers on this Protocol MUST be idempotent (per
    foundation r3 dispatcher-idempotence invariant). Every mutation
    method returns silently on a duplicate `event_id`.
    """

    card_id: CardId
    kind: str
    applied_appends: _BoundedLRUSet
    applied_snapshots: _BoundedLRUSet
    state: str  # "initializing" | "running" | ... | "success" | "error" | "cancelled"
    content: str

    def apply_append(self, event_id: str, chunk: str) -> None:
        """Idempotent — same `event_id` twice is a no-op."""
        ...

    def apply_snapshot(self, event_id: str, content: str) -> None:
        """Idempotent — same `event_id` twice is a no-op."""
        ...

    def apply_status(self, status: str) -> None:
        """Last-writer-wins on `state`."""
        ...

    def apply_close(
        self,
        final_status: str,
        error_message: str | None,
    ) -> None:
        """Idempotent — a card that's already terminal stays put."""
        ...


class _CardMountStub:
    """spec-006 stub impl of `CardMountState`.

    Accumulates content in memory; no DOM mount. Handlers exercise the
    idempotence contract on this stub; swapping in spec-007's
    widget-backed impl preserves the invariants because the seam is
    the Protocol.
    """

    __slots__ = (
        "applied_appends",
        "applied_snapshots",
        "card_id",
        "content",
        "error_message",
        "kind",
        "state",
    )

    def __init__(self, card_id: CardId, kind: str) -> None:
        self.card_id = card_id
        self.kind = kind
        self.applied_appends: _BoundedLRUSet = _BoundedLRUSet(
            maxsize=_CARD_APPEND_LRU_MAXSIZE,
        )
        self.applied_snapshots: _BoundedLRUSet = _BoundedLRUSet(
            maxsize=_CARD_SNAPSHOT_LRU_MAXSIZE,
        )
        self.state: str = "initializing"
        self.content: str = ""
        self.error_message: str | None = None

    def apply_append(self, event_id: str, chunk: str) -> None:
        """Apply an appended chunk; idempotent by `event_id`."""
        if event_id in self.applied_appends:
            return
        self.applied_appends.add(event_id)
        self.content += chunk
        if self.state == "initializing":
            self.state = "running"

    def apply_snapshot(self, event_id: str, content: str) -> None:
        """Apply a snapshot — replace content, clear applied_appends."""
        if event_id in self.applied_snapshots:
            return
        self.applied_snapshots.add(event_id)
        self.content = content
        # Snapshot supersedes prior appends; the LRU is cleared so a
        # post-snapshot non-superseded append still applies cleanly.
        self.applied_appends = _BoundedLRUSet(
            maxsize=_CARD_APPEND_LRU_MAXSIZE,
        )
        if self.state == "initializing":
            self.state = "running"

    def apply_status(self, status: str) -> None:
        """Last-writer-wins."""
        self.state = status

    def apply_close(
        self,
        final_status: str,
        error_message: str | None,
    ) -> None:
        """Terminal transition — no-op once already terminal."""
        if self.state in ("success", "error", "cancelled"):
            return
        self.state = final_status
        self.error_message = error_message


class CardTrackingEntry(_CardMountStub):
    """spec-006 backward-compat alias for `_CardMountStub`.

    Retained for the one-milestone transition window until spec-007
    lands the widget-backed impl. New code uses `_CardMountStub` or
    the spec-007 impl directly.
    """


class _UIEventDataView:
    """Duck-typed view over an `Event` + payload for widget dispatch.

    `CardWidget.apply_event` / body renderers expect an object with
    `.kind`, `.payload`, and `.event_id`. The adapter synthesises
    this view from the incoming `Event` + payload dict so the
    spec-007 widget layer can be fed without materialising a full
    `UIEventData` schema instance.
    """

    __slots__ = ("event_id", "kind", "payload")

    def __init__(self, event: Event, payload: dict[str, Any]) -> None:
        data = event.data if isinstance(event.data, dict) else {}
        kind_val = data.get("kind")
        self.kind = kind_val if isinstance(kind_val, str) else ""
        self.payload = payload
        self.event_id = str(event.event_id)


# ---------------------------------------------------------------------------
# Helpers ported from `_renderer.py`
# ---------------------------------------------------------------------------


def _extract_text(content: dict[str, object] | None) -> str:
    """Pull the display string out of an event's ``content`` dict.

    Ported verbatim from ``Renderer._extract_text`` at
    ``_renderer.py:594``. Both ``agent.message`` and
    ``model.response`` carry their rendered text under
    ``content.text``.
    """
    if not content:
        return ""
    text = content.get("text")
    if isinstance(text, str):
        return text
    if text is None:
        return ""
    return str(text)


def _event_id_str(event: Any) -> str | None:
    """bug-036d: safe ``event_id`` accessor for sink method dedupe keys.

    Real pydantic :class:`Event` values carry a string-newtype
    ``event_id``; the wire-reconstructed sub-agent event stub
    (``_ReconstructedSubAgentEvent``) may leave it ``None`` when
    the outer envelope omits the field. Returns a stringified id or
    ``None`` so the row's ``_mounted_event_ids`` doesn't dedupe on
    the literal string ``"None"``.
    """
    event_id = getattr(event, "event_id", None)
    if event_id is None:
        return None
    return str(event_id)


def _count_row_lifecycle(sink: Any) -> None:
    """bug-036d AC #8: bump a row sink's lifecycle-event counter.

    Lifecycle events (``config.set``, ``thread.exchange_*``,
    ``agent.template_warning``) don't have a widget mount path but
    still count toward the row's "N sub-agent events" label. When
    the sink is a workflow row (not the main App), forward the
    counter bump via the row's ``count_lifecycle_event`` hook.
    """
    hook = getattr(sink, "count_lifecycle_event", None)
    if callable(hook):
        with contextlib.suppress(Exception):
            hook()


def _content_dict_text(content: dict[str, object]) -> str:
    """bug-036d fallback: readable text for a non-``text``-keyed content.

    Workflow-invoked agents receive their bound inputs under
    ``data.content = {"job_description": "..."}`` — no ``text``
    key means ``_extract_text`` returns ``""``. The row's sink
    still wants something to render (Mockup 4 renders each key
    on its own labeled line). Delegates to the row-module helper
    that already handles bash-shape + generic dict rendering.
    """
    try:
        from cli.chat.widgets.workflow_node_row import (  # noqa: PLC0415
            _extract_content_text as _row_extract,
        )
    except Exception:  # noqa: BLE001 — widget layer optional
        return ""
    result = _row_extract(content)
    return result if isinstance(result, str) else ""


_PANEL_HEADER_ARG_MAX_LEN = 60
"""Match `_renderer.py:674` — truncation width for arg values so
the tool-block header stays one line."""


def _format_args(args: dict[str, object] | None) -> str:
    """Render a tool's arguments as ``key=val, key=val`` for the header.

    Ported verbatim from ``_renderer.py:680``. Strings get repr'd
    so the user sees the quotes; other types ``str()`` through;
    long values truncate with ``...``.
    """
    if not args:
        return ""
    parts: list[str] = []
    for key, value in args.items():
        rendered = repr(value) if isinstance(value, str) else str(value)
        if len(rendered) > _PANEL_HEADER_ARG_MAX_LEN:
            rendered = rendered[: _PANEL_HEADER_ARG_MAX_LEN - 3] + "..."
        parts.append(f"{key}={rendered}")
    return ", ".join(parts)


def _format_tool_result(result: dict[str, object] | None) -> str:
    """Render a tool.result payload as displayable text.

    Ported from ``_renderer.py:700``.
    """
    if not result:
        return ""
    for key in ("content", "stdout", "text", "value"):
        if key in result:
            value = result[key]
            return value if isinstance(value, str) else str(value)
    try:
        return json.dumps(result, sort_keys=True, indent=2, default=str)
    except (TypeError, ValueError):
        return str(result)


def _detect_code_blocks(text: str) -> list[tuple[str, str]]:
    """Split ``text`` into a sequence of ``(kind, body)`` chunks.

    ``kind`` is ``"prose"`` or ``"code"``. Ported from the inline
    heuristic at ``_renderer.py:103–149``; extracted here so the
    adapter can be tested in isolation without spinning up a
    ``Renderer``.

    Rules mirror the source:

    - Triple-backtick fences delimit code chunks.
    - The language label after the opening fence is ignored here;
      Textual's ``Markdown`` widget handles that natively.
    - A closing fence returns to prose. Any residual code with no
      closing fence stays as a code chunk to end-of-text.
    """
    chunks: list[tuple[str, str]] = []
    current_kind = "prose"
    current_lines: list[str] = []
    for raw_line in text.split("\n"):
        if raw_line.startswith("```"):
            if current_lines:
                chunks.append((current_kind, "\n".join(current_lines)))
                current_lines = []
            current_kind = "code" if current_kind == "prose" else "prose"
            continue
        current_lines.append(raw_line)
    if current_lines:
        chunks.append((current_kind, "\n".join(current_lines)))
    return chunks


def _now_ts(event: Event) -> datetime:
    """Return a ``datetime`` for the event's timestamp.

    ``Event.created_at`` is the canonical field; fall back to
    ``datetime.now(UTC)`` if the field is missing (should never
    happen but the adapter is defensive).
    """
    ts = getattr(event, "created_at", None)
    if isinstance(ts, datetime):
        return ts
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class EventAdapter:
    """Dispatch ``Event``s to widget public methods.

    Constructor takes the ``ChatApp`` so handlers can call
    ``app.mount_*`` helpers, query the transcript, or read app-level
    state. The adapter carries only ephemeral bookkeeping
    (tool-args by call id; streaming widgets by call id).
    """

    # Class-level dispatch table: EventType → method name. Missing
    # entries fail Test 5 (event-coverage smoke) — every EventType
    # member must have a branch.
    _DISPATCH: dict[EventType, str] = {
        EventType.SESSION_OPENED: "_handle_session_opened",
        EventType.THREAD_EXCHANGE_STARTED: "_handle_thread_exchange_started",
        EventType.THREAD_EXCHANGE_COMPLETED: "_handle_thread_exchange_completed",
        EventType.THREAD_EXCHANGE_FAILED: "_handle_thread_exchange_failed",
        EventType.USER_MESSAGE: "_handle_user_message",
        EventType.AGENT_MESSAGE: "_handle_agent_message",
        EventType.TOOL_CALLED: "_handle_tool_called",
        EventType.TOOL_RESULT: "_handle_tool_result",
        EventType.TOOL_FAILED: "_handle_tool_failed",
        EventType.MODEL_REQUEST: "_handle_model_request",
        EventType.MODEL_RESPONSE: "_handle_model_response",
        EventType.MODEL_STREAMING_TOKEN: "_handle_model_streaming_token",
        EventType.MODEL_FALLBACK: "_handle_model_fallback",
        EventType.CONFIG_SET: "_handle_config_set",
        EventType.MEMORY_WRITTEN: "_handle_memory_written",
        EventType.MEMORY_RETRIEVED: "_handle_memory_retrieved",
        EventType.MEMORY_DELETED: "_handle_memory_deleted",
        EventType.CONTEXT_COMPACTED: "_handle_context_compacted",
        # feat-054 Phase 1: stub branches keep the coverage smoke green.
        # Notice widgets + rich rendering land in Phase 3 alongside the
        # slash-command surface (/compact, /uncompact, /compaction-history,
        # /why-compact); the stubs mean a Phase-2 emitter won't crash the
        # adapter dispatch table.
        EventType.CONTEXT_COMPACTION_REVOKED: "_handle_context_compaction_revoked",
        EventType.CONTEXT_COMPACTION_FAILED: "_handle_context_compaction_failed",
        EventType.CONTEXT_COMPACTION_SKIPPED: "_handle_context_compaction_skipped",
        EventType.AGENT_TEMPLATE_WARNING: "_handle_agent_template_warning",
        # spec-002 registers the UI event envelope. Emission +
        # per-kind dispatch land in spec-003 / spec-006 / spec-012+;
        # the adapter's branch here is a silent no-op so replay of a
        # future JSONL row doesn't raise `KeyError` on the dispatch
        # table lookup.
        EventType.UI_EVENT: "_handle_ui_event",
    }

    def __init__(
        self,
        app: ChatApp,
        *,
        session_store: SessionStore | None = None,
        session_id: SessionId | None = None,
        thread_id: ThreadId | None = None,
        session_meta: SessionMeta | None = None,
        prompts_impl: PromptsImpl | None = None,
        cards: Any | None = None,
        workflows_impl: WorkflowsImpl | None = None,
    ) -> None:
        self._app = app
        # tool_call_id → arguments dict (captured on TOOL_CALLED so
        # TOOL_RESULT / TOOL_FAILED can render the full signature).
        self._tool_call_args: dict[str, dict[str, Any]] = {}
        # TODO(bug-036a): bound this set — currently grows for the
        # session lifetime.
        # bug-036a: tool_call_ids whose transcript-root ToolCallBlock
        # was suppressed on TOOL_CALLED because the tool mounts its
        # own card. Consulted on TOOL_RESULT to skip the matching
        # result-side mount.
        self._suppressed_tool_calls: set[str] = set()
        # call_id → AssistantProse widget (streaming assistant text).
        # Populated on first MODEL_STREAMING_TOKEN for a call; cleared
        # on MODEL_RESPONSE for the same call.
        self._streaming_widgets: dict[str, Any] = {}
        # Concatenated deltas per call_id (for the "concat matches
        # final text" defensive check on MODEL_RESPONSE).
        self._streaming_buffers: dict[str, str] = {}
        # spec-004: idempotence LRUs for the prompt-routing branch of
        # `_handle_ui_event`. `_applied_prompt_ids` guards duplicate
        # `.requested` dispatches from mounting a second widget;
        # `_resolved_prompt_ids` guards duplicate `.answered` /
        # `.cancelled` dispatches from double-resolving the future
        # (would raise `InvalidStateError`).
        self._applied_prompt_ids: _BoundedLRUSet = _BoundedLRUSet(
            maxsize=_PROMPT_LRU_MAXSIZE,
        )
        self._resolved_prompt_ids: _BoundedLRUSet = _BoundedLRUSet(
            maxsize=_PROMPT_LRU_MAXSIZE,
        )
        # spec-004: handle to the concrete Prompts service so the
        # adapter's prompt routing can resolve pending futures. Set
        # by `ChatApp.on_mount` once `PromptsImpl` is registered; None
        # when the adapter runs without a live prompts service
        # (headless replay-only paths, spec-003 tests).
        self._prompts_impl: PromptsImpl | None = prompts_impl
        # spec-014: reference to the concrete `WorkflowsImpl` so the
        # adapter's prompt-routing branch can emit the durable
        # `awaiting_approval` / `approval_resolved` UI events when a
        # workflow-scoped approval fires. `None` in headless / tests
        # that never boot workflows. `PromptsImpl` remains unaware of
        # workflows (Coordinator #2).
        self._workflows_impl: WorkflowsImpl | None = workflows_impl
        # spec-014: prompt_id → (run_id, node_name) so
        # `_handle_prompt_answered` / `_handle_prompt_cancelled` can
        # emit `emit_approval_resolved` without re-consulting the
        # source dict (the `.answered` / `.cancelled` payloads don't
        # carry `source` by spec-004 shape).
        self._workflow_prompt_sources: dict[str, tuple[str, str]] = {}

        # spec-006/007: per-card tracking for the `_handle_card_*`
        # handlers. `_card_widgets` keys are `CardId`; values are
        # `CardMountState` Protocol impls — spec-006 shipped
        # `_CardMountStub`; spec-007 promotes to `CardWidget`
        # (widget-backed) via the Protocol seam. Handler code below
        # is UNCHANGED from spec-006 apart from `_handle_card_opened`,
        # which now also mounts the widget in the transcript.
        # `_applied_notice_ids` dedupes `kolsch.card.notice` events.
        self._card_widgets: dict[CardId, CardMountState] = {}
        # bug-033: `(trace_id, invocation_id)` → live `WorkflowNodeRow`
        # map. Populated by `WorkflowRenderer._on_node_started` via
        # `register_adapter`; popped by the same renderer on any
        # terminal node event (`NODE_COMPLETED` / `NODE_FAILED` /
        # `NODE_PRUNED`). Session-event handlers
        # (`_handle_agent_message`, `_handle_tool_called`,
        # `_handle_user_message`, `_handle_tool_result`) consult this
        # map: when `(event.trace_id, event.invocation_id)` matches a
        # live node, dispatch to
        # `WorkflowNodeRow.apply_sub_agent_event` and short-circuit
        # the default transcript-root mount.
        #
        # Pre-bug-033 this was keyed on `trace_id` alone (spec-013),
        # but `trace_id` is run-wide (executor.py:1321-1328): every
        # workflow node in a single run shares it, so sibling
        # NODE_STARTED events clobbered each other's registrations
        # and NODE_COMPLETED for one node evicted a still-live
        # sibling. The `(trace_id, invocation_id)` composite key
        # gives per-node uniqueness while keeping the trace scoping.
        #
        # Fallback: payloads that predate the `invocation_id` hoist
        # (very old replay data) fall back to `(trace_id, "")` — the
        # writer logs a WARNING. See `_register_node_with_adapter`
        # in `widgets/card_body/workflow.py`.
        #
        # Typed as `dict[tuple[str, str], Any]` (rather than
        # `dict[tuple[TraceId, InvocationId], WorkflowNodeRow]`) to
        # keep this adapter free of an upward dependency on
        # `workflows/`; the concrete keys are string subclasses and
        # the value is the row widget.
        self._live_nodes_by_trace_and_invocation: dict[tuple[str, str], Any] = {}
        self._applied_notice_ids: _BoundedLRUSet = _BoundedLRUSet(
            maxsize=_CARD_NOTICE_LRU_MAXSIZE,
        )
        # spec-008: notice_id → mounted widget (either an in-card
        # ``NoticeWidget`` inside a ``NoticeSlot`` or a
        # transcript-root ``NoticeWidget``). Used by the update-in-
        # place path: a repeat dispatch with the same notice_id calls
        # ``widget.update_text(new_text)`` rather than mounting a
        # second widget. Simple dict — bounded implicitly by
        # ``_applied_notice_ids`` LRU (once the id evicts, a later
        # dispatch is treated as fresh; the stale widget entry stays
        # in the dict until eviction happens on the next add — for
        # test purposes we don't age the widget dict independently).
        self._notice_widgets: dict[str, Any] = {}

        # feat-054 CompactionBlock tracking. Keys:
        # - "pending" — the widget mounted in `running` state by a
        #   ChatApp call site (manual /compact, eager-on-open) before
        #   the terminal event arrives. Consumed and transitioned by
        #   the terminal handler; deleted after use.
        # - "<event_id>" — the widget for a completed compaction,
        #   kept so `/uncompact` / `/why-compact` UX (future) can
        #   correlate.
        self._compaction_widgets: dict[str, Any] = {}

        # spec-008: reference to the ``Cards`` service so the
        # boot-time interruption notice (fired from
        # ``_scan_dangling_prompts``) can go through the same seam
        # ``Cards.notice(kind="warning")`` uses everywhere else.
        # Populated by ``ChatApp.on_mount`` after the service registry
        # comes up; ``None`` in headless / replay-only tests.
        self._cards: Any | None = cards

        # spec-003 bookmark state. Optional deps — when not wired,
        # `start()` is a no-op. `ChatApp` today constructs the adapter
        # without a session_store handle; the future integration will
        # wire it and call `start()` before subscribing to the live
        # bus (see docs/explanation/event-bus-contract.md §Replay-on-boot).
        self._session_store: SessionStore | None = session_store
        self._session_id: SessionId | None = session_id
        self._thread_id: ThreadId | None = thread_id
        self._bookmark: EventId | None = (
            session_meta.event_adapter_bookmark if session_meta is not None else None
        )
        self._bookmark_dispatch_count = 0
        self._bookmark_persist_every = 100
        # Coordinator decision Q4: idempotency guard. A second
        # `start()` returns early without re-dispatching.
        self._started = False
        # Coordinator decision Q3: bounded scan window.
        self._scan_tail = 1000
        # bug-056: point-in-time snapshot of the replayed slice's
        # `.requested` → outcome map. `stage_replay_prompt_outcomes` is
        # called BEFORE the replay dispatch loop (both `_replay_history`
        # and the dormant `_replay_from_bookmark`) with the exact slice
        # about to be handed to `handle()`; the map indexes every
        # `.requested` in the slice to one of ``("allowed", footer)``,
        # ``("denied", footer)``, ``("interrupted", footer)`` derived
        # from the matching `.answered` / `.cancelled` (or absent one —
        # dangling). `_handle_prompt_requested` consults this map: an
        # entry means "render as terminal, don't spawn the bridge, don't
        # freeze the composer"; the entry is popped on consumption so a
        # duplicate dispatch of the same `.requested` hits the standard
        # `_applied_prompt_ids` idempotence guard. Populated `None` (no
        # pre-pass staged) is the live path — any `.requested` there is
        # a real in-flight prompt and takes the interactive branch.
        # AC #12 / AC #13: because the map is a point-in-time snapshot,
        # a post-resume live `.requested` arriving on the live bus after
        # replay has no entry and takes the interactive path.
        self._replay_prompt_outcomes: dict[str, tuple[str, str]] = {}

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @classmethod
    def handles(cls, event_type: EventType) -> bool:
        """True if a handler is registered for ``event_type``."""
        return event_type in cls._DISPATCH

    async def dispatch_sub_agent_event(
        self,
        inner_event: Any,
        *,
        outer_trace_id: str | None = None,
    ) -> None:
        """bug-036d: unwrap + re-dispatch a sub-agent event through the sink.

        Called by both call sites of the (deleted)
        ``row.apply_sub_agent_event``:

        - :meth:`_handle_ui_event`'s sub-agent branch reconstructs
          the inner event from the outer envelope's payload and
          hands it here.
        - :class:`~cli.chat.widgets.card_body.workflow.WorkflowRenderer`'s
          ``_on_sub_agent_event`` does the same when the renderer
          receives a fan-out.

        Trace-id safety (Reviewer #1 finding): if
        ``outer_trace_id`` is supplied, it must match the inner
        event's ``trace_id``. A mismatch drops the event with a
        WARNING log — prevents a re-dispatch that would mis-route
        into a different row than the outer envelope was addressed
        to.
        """
        inner_trace = getattr(inner_event, "trace_id", None)
        if (
            outer_trace_id is not None
            and isinstance(inner_trace, str)
            and inner_trace != outer_trace_id
        ):
            _LOGGER.warning(
                "event_adapter: dispatch_sub_agent_event trace mismatch "
                "outer=%r inner=%r — dropped",
                outer_trace_id,
                inner_trace,
            )
            return
        try:
            await self.handle(inner_event)
        except Exception:
            _LOGGER.warning(
                "event_adapter: sub-agent handler raised for %r",
                getattr(inner_event, "event_id", "<unknown>"),
                exc_info=True,
            )

    def register_cards_impl(self, cards: Any) -> None:
        """Wire a live ``Cards`` handle after construction.

        Mirrors ``register_prompts_impl``: the adapter is built before
        ``ChatApp.on_mount`` constructs the concrete services (Textual
        lifecycle constraint), so the ``cards=`` kwarg is optional at
        ``__init__`` time and this setter closes the loop. Used by
        the boot-time interruption notice from
        ``_scan_dangling_prompts`` when the session's replay
        synthesises at least one ``tool.failed``.
        """
        self._cards = cards

    def register_workflows_impl(self, workflows_impl: WorkflowsImpl) -> None:
        """Wire a live ``WorkflowsImpl`` in after construction.

        spec-014: mirror of ``register_prompts_impl``. Populated by
        ``ChatApp.on_mount`` once the service registry is up. Enables
        the adapter's workflow-source prompt branch to emit the
        durable ``awaiting_approval`` / ``approval_resolved`` UI
        events without ``PromptsImpl`` learning about workflows
        (Coordinator #2 lock).
        """
        self._workflows_impl = workflows_impl

    def register_prompts_impl(self, prompts_impl: PromptsImpl) -> None:
        """Wire a live `PromptsImpl` in after construction.

        `ChatApp.__init__` builds the adapter before `on_mount`
        constructs the concrete services (order is a Textual
        lifecycle constraint — some services need the session
        bundle handles the app receives at mount time). This setter
        closes the loop from `ChatApp.on_mount` so the adapter's
        `.answered` / `.cancelled` routing can reach the impl's
        pending future.
        """
        self._prompts_impl = prompts_impl

    async def handle(self, event: Event) -> None:
        """Dispatch ``event`` to the registered handler.

        Missing entries would raise ``KeyError``; the coverage test
        makes that a static failure.
        """
        method_name = self._DISPATCH[event.type]
        handler = getattr(self, method_name)
        await handler(event)

    def stage_replay_prompt_outcomes(self, events: list[Event]) -> None:
        """Index a replay slice's `.requested` → historical outcome.

        bug-056: called BEFORE the replay dispatch loop with the exact
        slice about to be handed to `handle()`. Walks the slice once,
        building a `{prompt_id → (kind, footer)}` map where
        ``kind ∈ {"allowed", "denied", "interrupted"}``; footer follows
        the interactive path's `_resolve` mapping (see
        `approval_block.py:283-390`):

        - `y` → ``("allowed", "✓ Allowed once")``
        - `a` + rule dict → ``("allowed", "✓ Rule saved (allow)")``
        - `n` / dropped `.cancelled` payload → ``("denied", "✗ Denied once")``
        - `d` + rule dict → ``("denied", "✗ Rule saved (deny)")``
        - `.requested` with no `.answered`/`.cancelled` in-slice →
          ``("interrupted", "⊘ Interrupted")``

        `_handle_prompt_requested` consults this map: an entry means
        "render as terminal, no bridge, no freeze". The map is a
        point-in-time snapshot — a post-resume live prompt arriving on
        the live bus has no entry and takes the interactive path
        (AC #13).

        Overwrites any previous staging so a second resume boot (or a
        `_replay_from_bookmark` call after `_replay_history`) starts
        clean; the caller owns which slice is staged.

        Note: `_scan_dangling_prompts` (test-only path) does NOT run on
        the production resume path, so this method is where dangling
        historical prompts get their `interrupted` synthesis for the
        UI (AC #6). The scan path stays untouched — it targets the
        durable log, not the render.
        """
        outcomes: dict[str, tuple[str, str]] = {}
        # First pass: index every terminal event for a prompt.
        for event in events:
            if event.type != EventType.UI_EVENT:
                continue
            data = event.data if isinstance(event.data, dict) else {}
            kind = data.get("kind")
            payload = data.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            prompt_id = payload.get("prompt_id")
            if not isinstance(prompt_id, str) or not prompt_id:
                continue
            if kind == WIRE_PROMPT_ANSWERED:
                # `.answered` payload key fallback matches
                # `_handle_prompt_answered` (`response` OR `answer`).
                response = payload.get("response", payload.get("answer", ""))
                if not isinstance(response, str):
                    response = str(response)
                rule = payload.get("rule")
                rule_present = isinstance(rule, dict)
                outcomes[prompt_id] = _replay_outcome_from_response(
                    response, has_rule=rule_present,
                )
            elif kind == WIRE_PROMPT_CANCELLED:
                # `.cancelled` payload has no `response`/`answer` key —
                # synthesise denied so the row reads the same footer as
                # an Esc / `n` interactive resolution.
                outcomes[prompt_id] = ("denied", "✗ Denied once")
        # Second pass: dangling `.requested` (in-slice, no resolution)
        # → interrupted. Must come after the terminal pass so an in-
        # slice `.answered` for a prompt wins over the interrupted
        # fallback.
        for event in events:
            if event.type != EventType.UI_EVENT:
                continue
            data = event.data if isinstance(event.data, dict) else {}
            if data.get("kind") != WIRE_PROMPT_REQUESTED:
                continue
            payload = data.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            prompt_id = payload.get("prompt_id")
            if not isinstance(prompt_id, str) or not prompt_id:
                continue
            if prompt_id not in outcomes:
                outcomes[prompt_id] = ("interrupted", "⊘ Interrupted")
        self._replay_prompt_outcomes = outcomes

    # ------------------------------------------------------------------
    # spec-003: replay-on-boot bookmark + prompt-mid-crash synthesis
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Idempotent startup — replay from bookmark, then scan for dangling prompts.

        Must be called before the live bus subscription so no events
        between the last-dispatched bookmark and the log tail are
        silently dropped. Second call returns early (Coordinator
        decision Q4 for spec-003).

        No-op when the store/session/thread deps aren't wired — the
        chat runtime today constructs the adapter without them; the
        wiring lands together with the concrete service impls in M2+.
        """
        if self._started:
            return
        self._started = True
        if (
            self._session_store is None
            or self._session_id is None
            or self._thread_id is None
        ):
            return
        await self._replay_from_bookmark()
        await self._scan_dangling_prompts()

    async def _replay_from_bookmark(self) -> None:
        """Read events from bookmark to log tail; dispatch each; persist every 100.

        Missing / stale bookmark (points at a non-existent event_id):
        logs a warning and replays the entire log. This matches AC #6.

        Handler exceptions are swallowed and logged — replay is
        best-effort, a bad event shouldn't crash the chat (mirrors
        `ChatApp._replay_history`).
        """
        assert self._session_store is not None
        assert self._session_id is not None
        assert self._thread_id is not None

        # First, sanity-check the bookmark by scanning the full log
        # for the bookmark event. If the bookmark points at an event
        # that doesn't exist (log rotated / corrupted meta), warn and
        # fall back to a full replay.
        replay_from: EventId | None = self._bookmark
        if self._bookmark is not None:
            all_events: list[Event] = []
            async for ev in self._session_store.read_events(self._thread_id):
                all_events.append(ev)
            if not any(str(e.event_id) == str(self._bookmark) for e in all_events):
                _LOGGER.warning(
                    "event_adapter bookmark %r not found in session %r log; "
                    "replaying entire log",
                    self._bookmark,
                    self._session_id,
                )
                replay_from = None

        # bug-056 AC #12: the dormant bookmark-replay path is a replay
        # too — stage the pre-pass here as well so a `.requested` in
        # this slice renders terminal instead of active. Buffer the
        # slice so we can index it before dispatching.
        replay_slice: list[Event] = []
        async for ev in self._session_store.read_events(
            self._thread_id,
            from_event_id=str(replay_from) if replay_from is not None else None,
        ):
            replay_slice.append(ev)
        self.stage_replay_prompt_outcomes(replay_slice)

        for event in replay_slice:
            try:
                await self.handle(event)
            except Exception:
                _LOGGER.exception(
                    "event_adapter handler raised during replay of %r",
                    event.event_id,
                )
            self._bookmark = event.event_id
            self._bookmark_dispatch_count += 1
            if self._bookmark_dispatch_count >= self._bookmark_persist_every:
                await self._persist_bookmark()

    async def _scan_dangling_prompts(self) -> None:
        """Synthesise `.cancelled` + `tool.failed` for unresolved prompt requests.

        Bounded scan (Coordinator decision Q3): only the last
        `self._scan_tail` events (default 1000) are considered. A
        dangling prompt is a `kolsch.prompt.requested` UI event with
        no matching `.answered` or `.cancelled` in the same window.
        For each dangling prompt:

        - Append synthetic `kolsch.prompt.cancelled` with
          `reason="session_interrupted"`.
        - If the parent `tool.called` (looked up via
          `source.tool_call_id` in the request's payload) has no
          `tool.result` or `tool.failed`, append synthetic
          `tool.failed` with `error.type="session.interrupted"`.

        Idempotent: on the second boot after synthesis, the newly-
        appended `.cancelled` closes the request, so the scan finds
        no dangling entries and does nothing. Pre-spec-004 (no
        `kolsch.prompt.requested` events exist anywhere), this is a
        silent no-op.
        """
        assert self._session_store is not None
        assert self._session_id is not None
        assert self._thread_id is not None

        events: list[Event] = []
        async for ev in self._session_store.read_events(
            self._thread_id,
            tail=self._scan_tail,
        ):
            events.append(ev)

        # First pass: index requested/resolved prompts + tool.called
        # + tool terminals within the scan window.
        requested: dict[str, Event] = {}
        resolved: set[str] = set()
        tool_calls: dict[str, Event] = {}
        tool_terminals: set[str] = set()

        for ev in events:
            if ev.type == EventType.UI_EVENT:
                data = ev.data if isinstance(ev.data, dict) else {}
                kind = data.get("kind")
                payload = data.get("payload") or {}
                if not isinstance(payload, dict):
                    payload = {}
                prompt_id = payload.get("prompt_id")
                if not isinstance(prompt_id, str):
                    continue
                if kind == WIRE_PROMPT_REQUESTED:
                    requested[prompt_id] = ev
                elif kind in (WIRE_PROMPT_ANSWERED, WIRE_PROMPT_CANCELLED):
                    resolved.add(prompt_id)
            elif ev.type == EventType.TOOL_CALLED:
                data = ev.data if isinstance(ev.data, dict) else {}
                call_id = data.get("tool_call_id")
                if isinstance(call_id, str):
                    tool_calls[call_id] = ev
            elif ev.type in (EventType.TOOL_RESULT, EventType.TOOL_FAILED):
                data = ev.data if isinstance(ev.data, dict) else {}
                call_id = data.get("tool_call_id")
                if isinstance(call_id, str):
                    tool_terminals.add(call_id)

        # Second pass: synthesize for dangling requests.
        tool_failed_synthesised = 0
        for prompt_id, req_ev in requested.items():
            if prompt_id in resolved:
                continue
            await self._append_synthetic_prompt_cancelled(prompt_id, req_ev)
            data = req_ev.data if isinstance(req_ev.data, dict) else {}
            source = data.get("payload", {}).get("source") if isinstance(
                data.get("payload"), dict
            ) else None
            tool_call_id = (
                source.get("tool_call_id")
                if isinstance(source, dict)
                else None
            )
            if (
                isinstance(tool_call_id, str)
                and tool_call_id in tool_calls
                and tool_call_id not in tool_terminals
            ):
                await self._append_synthetic_tool_failed(
                    tool_call_id,
                    tool_calls[tool_call_id],
                )
                tool_failed_synthesised += 1

        # spec-008: fire a boot-time interruption notice ONLY when we
        # synthesised at least one ``tool.failed``. Uses the same
        # ``Cards.notice`` seam every other in-transcript notice goes
        # through; the adapter's ``_handle_card_notice`` mounts a
        # ``NoticeWidget`` at the transcript root (no ``card_id`` in
        # the payload). Headless tests without a live ``Cards`` skip
        # silently.
        if tool_failed_synthesised > 0 and self._cards is not None:
            with contextlib.suppress(Exception):
                self._cards.notice(
                    "↻ Previous session was interrupted; ask the "
                    "assistant to retry.",
                    kind="warning",
                )

    async def _append_synthetic_prompt_cancelled(
        self,
        prompt_id: str,
        req_event: Event,
    ) -> None:
        """Append a synthetic `kolsch.prompt.cancelled` UI event.

        Reason is `session_interrupted`; payload also carries the
        `prompt_id` so downstream consumers can correlate with the
        original request.
        """
        assert self._session_store is not None
        assert self._thread_id is not None
        assert self._session_id is not None

        cancelled = Event(
            event_id=EventId(f"ev_{uuid4()}"),
            parent_event_id=None,
            thread_id=req_event.thread_id,
            session_id=req_event.session_id,
            type=EventType.UI_EVENT,
            data={
                "schema_version": 1,
                "kind": WIRE_PROMPT_CANCELLED,
                "payload": {
                    "prompt_id": prompt_id,
                    "reason": "session_interrupted",
                },
            },
            actor=Actor(kind="system"),
            created_at=datetime.now(UTC),
        )
        try:
            await self._session_store.append_event(self._thread_id, cancelled)
        except Exception:
            _LOGGER.exception(
                "failed to append synthetic prompt.cancelled for %r",
                prompt_id,
            )

    async def _append_synthetic_tool_failed(
        self,
        tool_call_id: str,
        tool_called: Event,
    ) -> None:
        """Append a synthetic `tool.failed` for an interrupted tool call.

        `error.type` is `session.interrupted`; the message explains
        the retry path so the model can pick up cleanly on its next
        turn.
        """
        assert self._session_store is not None
        assert self._thread_id is not None

        called_data = tool_called.data if isinstance(tool_called.data, dict) else {}
        tool_name = called_data.get("tool_name", "<unknown>")
        failed = Event(
            event_id=EventId(f"ev_{uuid4()}"),
            parent_event_id=None,
            thread_id=tool_called.thread_id,
            session_id=tool_called.session_id,
            type=EventType.TOOL_FAILED,
            data={
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "error": {
                    "type": "session.interrupted",
                    "message": (
                        "The chat session ended while this tool was awaiting "
                        "user input. Retry to continue."
                    ),
                },
            },
            actor=Actor(kind="system"),
            created_at=datetime.now(UTC),
        )
        try:
            await self._session_store.append_event(self._thread_id, failed)
        except Exception:
            _LOGGER.exception(
                "failed to append synthetic tool.failed for %r",
                tool_call_id,
            )

    async def _persist_bookmark(self) -> None:
        """Persist the current bookmark to the session's `meta.json`.

        Called every `_bookmark_persist_every` (default 100) dispatches
        during replay, and on `aclose()`. Best-effort — a failed write
        does not raise (see `SessionStore._write_meta_sync`).
        """
        if self._session_store is None or self._session_id is None:
            return
        await self._session_store.update_meta(
            self._session_id,
            event_adapter_bookmark=self._bookmark,
        )
        self._bookmark_dispatch_count = 0

    async def aclose(self) -> None:
        """Persist the bookmark on unmount. Idempotent."""
        if self._session_store is None or self._session_id is None:
            return
        await self._persist_bookmark()

    # ------------------------------------------------------------------
    # Handlers — one per EventType member
    # ------------------------------------------------------------------

    async def _handle_session_opened(self, event: Event) -> None:
        """Silent — session identity is already reflected in the InfoBar."""
        _ = event

    async def _handle_thread_exchange_started(self, event: Event) -> None:
        """Kick off the transient turn status.

        bug-036d: only fires the verb when the event is main-chat
        scoped. Sub-agent thread.exchange_started events count
        toward the row's activity counter but don't rewrite the
        outer chat's status line.
        """
        sink = self._route_to_workflow_node(event) or self._app
        if sink is self._app:
            await self._app.set_turn_verb("Thinking…", target="")
        else:
            _count_row_lifecycle(sink)

    async def _handle_thread_exchange_completed(self, event: Event) -> None:
        """Clear the transient turn status; the transcript speaks for itself."""
        sink = self._route_to_workflow_node(event) or self._app
        if sink is self._app:
            await self._app.set_turn_verb("", target="")
        else:
            _count_row_lifecycle(sink)

    async def _handle_thread_exchange_failed(self, event: Event) -> None:
        """Clear the turn status and surface the failure in the transcript.

        bug-036d: an exchange failure inside a workflow row routes
        the failure notice into the row instead of the transcript.
        """
        error = event.data.get("error") or {}
        message = str(error.get("message", "") or error.get("type", "exchange failed"))
        sink = self._route_to_workflow_node(event) or self._app
        if sink is self._app:
            await self._app.set_turn_verb("", target="")
            await self._app.mount_exchange_failed(message)
        else:
            await sink.mount_notice(
                f"exchange-failed-{event.event_id}",
                f"exchange failed — {message}",
                level="error",
                event_id=_event_id_str(event),
            )

    def _route_to_workflow_node(self, event: Event) -> Any:
        """Return the matching `WorkflowNodeRow`, or `None`.

        bug-036d: the return type flipped from ``bool`` to the
        row-or-``None`` shape so callers write::

            sink = self._route_to_workflow_node(event) or self._app
            await sink.mount_X(...)

        Same trace_id / invocation_id matching semantic as pre-036d;
        the difference is the caller now uses the returned row as a
        ``ChatMountSink`` rather than firing
        ``row.apply_sub_agent_event(event)`` inside this helper.

        spec-013 § trace_id routing (bug-033: extended to
        `(trace_id, invocation_id)`).
        """
        trace_id = getattr(event, "trace_id", None)
        if not isinstance(trace_id, str) or not trace_id:
            return None
        invocation_id = ""
        raw_data = getattr(event, "data", None)
        if isinstance(raw_data, dict):
            candidate = raw_data.get("invocation_id")
            if isinstance(candidate, str):
                invocation_id = candidate
        node_row = self._live_nodes_by_trace_and_invocation.get(
            (trace_id, invocation_id),
        )
        if node_row is None and invocation_id:
            node_row = self._live_nodes_by_trace_and_invocation.get(
                (trace_id, ""),
            )
        if node_row is None:
            for (mapped_trace, _mapped_inv), row in (
                self._live_nodes_by_trace_and_invocation.items()
            ):
                if mapped_trace == trace_id:
                    node_row = row
                    break
        return node_row

    async def _handle_user_message(self, event: Event) -> None:
        """Mount a ``UserChatbox`` for the just-submitted turn.

        bug-036d: routes to a workflow node row when trace_id
        matches. Inside a row, the USER_MESSAGE is the scheduler's
        bound-inputs invocation (e.g. ``{"job_description": "..."}``).
        """
        sink = self._route_to_workflow_node(event) or self._app
        content = event.data.get("content") or {}
        text = _extract_text(content)
        if not text:
            # Fallback: extract via the row's readable-dict path so
            # workflow bound-inputs (no ``text`` key) still render.
            if isinstance(content, dict) and content:
                text = _content_dict_text(content)
        if not text:
            return
        # bug-037: pass the raw dict through so row sinks can split
        # per-key (one ``input: <key>`` section per bound input). App
        # sinks ignore this kwarg.
        raw_content = content if isinstance(content, dict) and content else None
        await sink.mount_user_message(
            text, _now_ts(event), event_id=_event_id_str(event),
            content=raw_content,
        )

    async def _handle_agent_message(self, event: Event) -> None:
        """Mount assistant prose only if the sink didn't already.

        bug-036d: sink-based dedupe — the same
        ``last_assistant_text`` check the main chat runs now applies
        to workflow rows too. Card-mounting-tool suppression is
        adapter-scoped and still runs before dispatch.
        """
        sink = self._route_to_workflow_node(event) or self._app
        content = event.data.get("content") or {}
        text = _extract_text(content)
        if not text and isinstance(content, dict) and content:
            text = _content_dict_text(content)
        if not text:
            return
        if sink.last_assistant_text() == text:
            return
        await sink.mount_assistant_message(text, event_id=_event_id_str(event))
        sink.set_last_assistant_text(text)

    async def _handle_tool_called(self, event: Event) -> None:
        """Capture args + fire the turn verb on the main-chat surface.

        bug-036d: arg capture is adapter-scoped (survives across
        sink boundaries so ``_handle_tool_result`` can still look up
        the args). Turn verb is main-chat-only — sub-agent calls
        must not trample the outer chat's status line.
        """
        tool_call_id = str(event.data.get("tool_call_id", ""))
        if tool_call_id:
            self._tool_call_args[tool_call_id] = dict(
                event.data.get("arguments") or {},
            )
        tool_name = str(event.data.get("tool_name", ""))
        if not tool_name:
            return
        sink = self._route_to_workflow_node(event) or self._app
        # bug-036a: card-mounting tools skip the ToolCallBlock mount
        # (the tool opens its own card). Suppression is set on the
        # adapter regardless of sink so the matching TOOL_RESULT
        # can consult it.
        if _is_card_mounting_tool(tool_name) and tool_call_id:
            self._suppressed_tool_calls.add(tool_call_id)
        # bug-036d: only the main App has a turn verb. Sub-agent
        # calls inside a workflow row bump the row's counter via
        # the sub-agent bridge; touching the outer chat's verb
        # would trample it.
        if sink is self._app:
            args_summary = _format_args(event.data.get("arguments") or {})
            await self._app.set_turn_verb(
                f"Calling {tool_name}",
                target=args_summary,
            )
        else:
            # Row sink: TOOL_CALLED doesn't mount (main-chat parity —
            # the widget lands on TOOL_RESULT). Still bump the row's
            # activity counter so the collapsed title reflects that
            # a sub-agent tool call happened.
            _count_row_lifecycle(sink)

    async def _handle_tool_result(self, event: Event) -> None:
        """Mount a ``ToolCallBlock`` with the completed result.

        bug-036d: sink-based routing. bug-036a suppression precedes
        the dispatch so card-mounting sub-agent tools skip the
        row-scoped mount too.
        """
        tool_name = str(event.data.get("tool_name", "<unknown>"))
        tool_call_id = str(event.data.get("tool_call_id", ""))
        args = self._tool_call_args.pop(tool_call_id, {})
        args_str = _format_args(args)
        result = event.data.get("result") or {}
        suppressed = tool_call_id in self._suppressed_tool_calls
        card_id_present = isinstance(result, dict) and bool(result.get("card_id"))
        sink = self._route_to_workflow_node(event) or self._app
        if suppressed or _is_card_mounting_tool(tool_name) or card_id_present:
            self._suppressed_tool_calls.discard(tool_call_id)
            if sink is self._app:
                await self._app.set_turn_verb("Thinking…", target="")
            return
        body = _format_tool_result(result)
        await sink.mount_tool_call(
            tool_name, args_str, body,
            failed=False,
            event_id=_event_id_str(event),
        )
        if sink is self._app:
            await self._app.set_turn_verb("Thinking…", target="")

    async def _handle_tool_failed(self, event: Event) -> None:
        """Mount a ``ToolCallBlock`` with ``failed=True`` styling.

        bug-036d: routes through the sink — closes the bug-037 M1
        gap where a workflow's inner tool failure mounted the block
        at the transcript root instead of inside the owning row.
        """
        tool_name = str(event.data.get("tool_name", "<unknown>"))
        tool_call_id = str(event.data.get("tool_call_id", ""))
        args = self._tool_call_args.pop(tool_call_id, {})
        args_str = _format_args(args)
        error = event.data.get("error") or {}
        code = str(error.get("type", "") or error.get("code", "<unknown>"))
        message = str(error.get("message", ""))
        body = f"{code}: {message}" if code else message
        recovery_hint = event.data.get("recovery_hint")
        recovery_hint = recovery_hint if isinstance(recovery_hint, str) else None
        # feat-041 — pluck the authz matched_rule (when present) so the
        # ToolCallBlock body composition can append the 2-line denial
        # hint (spec §Mockup 1). Non-authz failures leave
        # `matched_rule` None and the body renders as before.
        detail = error.get("detail") or {}
        matched_rule_raw = (
            detail.get("matched_rule") if isinstance(detail, dict) else None
        )
        matched_rule = (
            matched_rule_raw if isinstance(matched_rule_raw, dict) else None
        )
        sink = self._route_to_workflow_node(event) or self._app
        await sink.mount_tool_call(
            tool_name,
            args_str,
            body,
            failed=True,
            recovery_hint=recovery_hint,
            matched_rule=matched_rule,
            event_id=_event_id_str(event),
        )

    async def _handle_model_request(self, event: Event) -> None:
        """MODEL_REQUEST fires the "Thinking…" verb on the main chat.

        bug-036d: turn verb is app-scoped — a sub-agent
        MODEL_REQUEST inside a workflow node doesn't rewrite the
        outer chat's status line.
        """
        sink = self._route_to_workflow_node(event) or self._app
        if sink is self._app:
            await self._app.set_turn_verb("Thinking…", target="")
        else:
            _count_row_lifecycle(sink)

    async def _handle_model_response(self, event: Event) -> None:
        """Finalise the streaming widget or mount a fresh one.

        bug-036d: MODEL_RESPONSE now routes to the sink like
        AGENT_MESSAGE. Inside a workflow row this fixes the
        visibility gap the user reported (rows previously looked
        "empty" between NODE_STARTED and NODE_COMPLETED).

        Streaming stays adapter-scoped — sub-agent
        MODEL_STREAMING_TOKEN events are dropped by
        ``_SUB_AGENT_EVENT_TYPES_DROP`` upstream, so the streaming
        widget lookup only ever hits for main-chat calls.
        """
        call_id = str(event.data.get("call_id", ""))
        content = event.data.get("content") or {}
        text = _extract_text(content)
        # Streaming path: the widget was mounted on the first
        # STREAMING_TOKEN. Streaming is app-only (no sub-agent
        # streaming exists), so this branch runs against the App.
        if call_id and call_id in self._streaming_widgets:
            self._streaming_widgets.pop(call_id, None)
            self._streaming_buffers.pop(call_id, None)
            if text:
                self._app.set_last_assistant_text(text)
            await self._app.set_turn_verb("Thinking…", target="")
            return
        sink = self._route_to_workflow_node(event) or self._app
        if text:
            # bug-036d dedupe against the sink's last-assistant-text
            # (matches _handle_agent_message shape). Prevents double-
            # mount when both MODEL_RESPONSE and AGENT_MESSAGE fire
            # with identical content.
            if sink.last_assistant_text() != text:
                await sink.mount_assistant_message(
                    text, event_id=_event_id_str(event),
                )
                sink.set_last_assistant_text(text)
        if sink is self._app:
            await self._app.set_turn_verb("Thinking…", target="")

    async def _handle_model_streaming_token(self, event: Event) -> None:
        """Append one delta to the assistant prose widget for this call."""
        call_id = str(event.data.get("call_id", ""))
        delta = str(event.data.get("text_delta", ""))
        if not call_id or not delta:
            return
        widget = self._streaming_widgets.get(call_id)
        if widget is None:
            widget = await self._app.mount_assistant_streaming_widget()
            self._streaming_widgets[call_id] = widget
            self._streaming_buffers[call_id] = ""
        self._streaming_buffers[call_id] += delta
        # `AssistantProse.append` awaits the underlying
        # `Markdown.append` — safe on the event-loop thread.
        await widget.append(delta)
        await self._app.set_turn_verb("Streaming…", target="")

    async def _handle_model_fallback(self, event: Event) -> None:
        """Surface the fallback transition as a dim italic notice.

        bug-036d: routes through the sink so a fallback fired inside
        a workflow node lands inside the owning row rather than
        orphaning at the transcript root.
        """
        to = event.data.get("to") or {}
        to_label = (
            f"{to.get('provider', '?')}/{to.get('model', '?')}"
            if isinstance(to, dict)
            else str(to)
        )
        reason = str(event.data.get("reason", "transient model failure"))
        notice_id = f"model-fallback-{event.event_id}"
        text = f"falling back to {to_label} because {reason}"
        sink = self._route_to_workflow_node(event) or self._app
        if sink is self._app:
            await self._mount_root_notice(notice_id, text, "info")
        else:
            await sink.mount_notice(
                notice_id, text, level="info",
                event_id=_event_id_str(event),
            )

    async def _handle_config_set(self, event: Event) -> None:
        """Silent — config is diagnostic-only in the JSONL.

        bug-036d AC #8: still bumps the row's lifecycle counter when
        the event is scoped to a workflow node (main-chat sink is a
        no-op).
        """
        sink = self._route_to_workflow_node(event) or self._app
        if sink is not self._app:
            _count_row_lifecycle(sink)

    async def _handle_memory_written(self, event: Event) -> None:
        """Silent — memory is diagnostic-only."""
        _ = event

    async def _handle_memory_retrieved(self, event: Event) -> None:
        """Silent — memory is diagnostic-only."""
        _ = event

    async def _handle_memory_deleted(self, event: Event) -> None:
        """Silent — memory is diagnostic-only."""
        _ = event

    async def _handle_context_compacted(self, event: Event) -> None:
        """feat-054 — mount / transition a CompactionBlock for the event.

        When a ``pending`` running block already exists (mounted by the
        ChatApp call site before the summariser call), transition it
        in place. Otherwise mount a fresh terminal block.
        """
        compacted_ids = event.data.get("compacted_event_ids") or []
        if not compacted_ids:
            return
        tokens_before = int(event.data.get("tokens_before") or 0)
        tokens_after = int(event.data.get("tokens_after") or 0)
        preview_flag = bool(event.data.get("preview"))
        trigger = str(event.data.get("trigger") or "auto")
        summary = event.data.get("summary") or {}
        refs_count = (
            len(summary.get("refs") or []) if isinstance(summary, dict) else None
        )
        eid_str = _event_id_str(event) or ""
        block = await self._acquire_compaction_block(event_id=eid_str)
        if block is None:
            return
        if preview_flag:
            block.transition_to_preview(
                compacted_count=len(compacted_ids),
                tokens_before=tokens_before,
                tokens_after=tokens_after,
            )
        else:
            block.transition_to_success(
                compacted_count=len(compacted_ids),
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                trigger=trigger,
                refs_count=refs_count,
            )

    async def _handle_context_compaction_revoked(self, event: Event) -> None:
        target = str(event.data.get("compaction_event_id") or "")
        short = target[:12] if target else "(unknown)"
        eid_str = _event_id_str(event) or ""
        block = await self._acquire_compaction_block(event_id=eid_str)
        if block is None:
            return
        block.transition_to_revoked(target_short_id=short)

    async def _handle_context_compaction_failed(self, event: Event) -> None:
        error = event.data.get("error") or {}
        message = ""
        if isinstance(error, dict):
            message = str(error.get("message") or error.get("type") or "")
        if not message:
            message = "unknown"
        fallback = str(event.data.get("fallback") or "") or None
        eid_str = _event_id_str(event) or ""
        block = await self._acquire_compaction_block(event_id=eid_str)
        if block is None:
            return
        block.transition_to_failure(message=message, fallback_tier=fallback)

    async def _handle_context_compaction_skipped(self, event: Event) -> None:
        reason = str(event.data.get("reason") or "unknown")
        eid_str = _event_id_str(event) or ""
        block = await self._acquire_compaction_block(event_id=eid_str)
        if block is None:
            return
        block.transition_to_skipped(reason=reason)

    async def _acquire_compaction_block(
        self, *, event_id: str
    ) -> Any:
        """Return the CompactionBlock to write into.

        Consumes a pre-mounted ``pending`` running block if present.
        Otherwise mounts a fresh block at the transcript root. Returns
        ``None`` on hard mount failure (headless test paths).
        """
        pending = self._compaction_widgets.pop("pending", None)
        if pending is not None:
            if event_id:
                self._compaction_widgets[event_id] = pending
            return pending
        transcript = self._locate_transcript()
        if transcript is None:
            return None
        try:
            from cli.chat.widgets.compaction_block import (  # noqa: PLC0415
                CompactionBlock,
            )
        except Exception:
            _LOGGER.exception("event_adapter: CompactionBlock import failed")
            return None
        try:
            block = CompactionBlock(state="running", event_id=event_id or None)
            await transcript.mount(block)
        except Exception:
            _LOGGER.exception(
                "event_adapter: CompactionBlock mount failed for event_id=%r",
                event_id,
            )
            return None
        if event_id:
            self._compaction_widgets[event_id] = block
        return block

    async def _handle_agent_template_warning(self, event: Event) -> None:
        """Surface template warnings as a dim inline notice.

        bug-036d: routes through the sink so a sub-agent template
        warning lands inside the owning workflow row.
        """
        message = str(event.data.get("message") or event.data.get("reason", ""))
        if not message:
            return
        notice_id = f"template-warning-{event.event_id}"
        text = f"template warning: {message}"
        sink = self._route_to_workflow_node(event) or self._app
        if sink is self._app:
            await self._mount_root_notice(notice_id, text, "warning")
        else:
            await sink.mount_notice(
                notice_id, text, level="warning",
                event_id=_event_id_str(event),
            )

    async def _handle_ui_event(self, event: Event) -> None:
        """Dispatch `ui.event` on `data.kind` to the per-kind route.

        spec-004 adds routing for the `kolsch.prompt.*` kinds:

        - `WIRE_PROMPT_REQUESTED` → mount the `ApprovalBlock` widget
          via `ChatApp.mount_approval_block`. Idempotent per
          `prompt_id` (a second dispatch is a no-op).
        - `WIRE_PROMPT_ANSWERED` → resolve the `PromptsImpl` future
          with the payload's `response`. Idempotent per `prompt_id`.
        - `WIRE_PROMPT_CANCELLED` → cancel the `PromptsImpl` future
          (raises `CancelledError` from the awaiting `choice()` —
          Coordinator-locked). Idempotent per `prompt_id`.

        Card / workflow-UI kinds route through their own services
        (spec-006 / spec-012+). Unknown kinds are a silent no-op so
        replay of a future JSONL row typed `ui.event` doesn't raise.
        """
        data = event.data if isinstance(event.data, dict) else {}
        kind = data.get("kind")
        payload = data.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}

        if kind == WIRE_PROMPT_REQUESTED:
            await self._handle_prompt_requested(payload)
            return
        if kind == WIRE_PROMPT_ANSWERED:
            self._handle_prompt_answered(payload)
            return
        if kind == WIRE_PROMPT_CANCELLED:
            self._handle_prompt_cancelled(payload)
            return
        # spec-006/007: card kinds route to the `_handle_card_*`
        # branch. Only `_handle_card_opened` is async (widget mount);
        # the other handlers stay sync per spec-006's Protocol seam.
        if kind == WIRE_CARD_OPENED:
            await self._handle_card_opened(event, payload)
            return
        if kind == WIRE_CARD_APPENDED:
            self._handle_card_appended(event, payload)
            return
        if kind == WIRE_CARD_SET:
            self._handle_card_set(event, payload)
            return
        if kind == WIRE_CARD_SET_PROGRESS:
            self._handle_card_set_progress(event, payload)
            return
        if kind == WIRE_CARD_STATUS:
            self._handle_card_status(event, payload)
            return
        if kind == WIRE_CARD_CLOSED:
            self._handle_card_closed(event, payload)
            return
        if kind == WIRE_CARD_SNAPSHOT:
            self._handle_card_snapshot(event, payload)
            return
        if kind == WIRE_CARD_NOTICE:
            self._handle_card_notice(event, payload)
            return
        if kind == WIRE_WORKFLOW_PROGRESS:
            self._handle_workflow_progress(event, payload)
            return
        if kind == WIRE_WORKFLOW_NODE_AWAITING_APPROVAL:
            self._handle_workflow_node_awaiting_approval(event, payload)
            return
        if kind == WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED:
            self._handle_workflow_node_approval_resolved(event, payload)
            return
        if kind == WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT:
            # bug-036d: fully async dispatch chain — unwrap the inner
            # event here and hand it to ``dispatch_sub_agent_event``.
            # The pre-036d fan-out kept the renderer path around for
            # multi-workflow layout, but the dispatch itself no
            # longer needs the renderer as intermediary (the adapter
            # owns the routing map). We still fan out to every
            # renderer so any per-renderer bookkeeping (e.g., row
            # counter registration) runs.
            await self._dispatch_sub_agent_ui_event(event, payload)
            return
        # Unknown or workflow-UI kinds land here as silent no-ops
        # until their service specs wire the routing.

    async def _handle_prompt_requested(self, payload: dict[str, Any]) -> None:
        """Mount the approval widget for a fresh `kolsch.prompt.requested`.

        Idempotent per `prompt_id` — a duplicate dispatch (from
        replay + live path racing, or a broken publisher) is silently
        dropped so the transcript never grows a second approval block
        for the same request.

        Extracts `rule_id` / `action` / `target` from the payload's
        `source` when available (the InlineApprovalCallback shape).
        Falls back to bare `action` / no target when the caller
        hasn't wired the extra fields — the widget renders `(no
        matching rule)` in that case.

        spec-014: when `source.kind == "workflow"` this handler ALSO
        fires `WorkflowsImpl.emit_awaiting_approval` (Coordinator #2
        lock — adapter routes, PromptsImpl unaware of workflows) and
        constructs the `ApprovalBlock` with a `parent_hint` derived
        from the workflow run's `ref` so the `↳` gutter connector
        renders above the border box.
        """
        prompt_id = payload.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            return
        if prompt_id in self._applied_prompt_ids:
            return
        self._applied_prompt_ids.add(prompt_id)
        rule_id = payload.get("rule_id")
        action = payload.get("action") or ""
        target = payload.get("target")
        rule_id_val = rule_id if isinstance(rule_id, str) else None
        action_val = action if isinstance(action, str) else ""
        target_val = target if isinstance(target, str) else None

        # bug-056: consult the replay pre-pass snapshot BEFORE the
        # spec-014 workflow-source branch so a historical workflow-
        # scoped prompt does NOT trigger a fresh `emit_awaiting_approval`
        # (which would append a new durable UI event on resume and
        # violate AC #4). Presence of an entry means the slice already
        # carries the historical outcome — render terminal (non-blocking,
        # no bridge, composer stays usable, no durable emit). Absence
        # means this is a genuinely live prompt (post-resume live bus,
        # or a fresh session tool call) — fall through to the workflow
        # branch + interactive mount + bridge. `.pop` so a duplicate
        # `.requested` dispatch (belt + braces alongside
        # `_applied_prompt_ids`) still no-ops.
        terminal_entry = self._replay_prompt_outcomes.pop(prompt_id, None)

        # spec-014: detect workflow-source prompts and derive the
        # parent_hint for the ApprovalBlock. The durable
        # `emit_awaiting_approval` fires only on the LIVE path — a
        # historical (terminal) prompt already has its matching
        # `.awaiting_approval` in the log slice and would produce a
        # duplicate emit on resume otherwise.
        source = payload.get("source")
        parent_hint: str | None = None
        if isinstance(source, dict) and source.get("kind") == "workflow":
            run_id_raw = source.get("run_id")
            node_name_raw = source.get("node_name")
            run_id = run_id_raw if isinstance(run_id_raw, str) else ""
            node_name = (
                node_name_raw if isinstance(node_name_raw, str) else ""
            )
            # Track the (run_id, node_name) tuple so the follow-up
            # `.answered` / `.cancelled` dispatch can fire
            # `emit_approval_resolved` without re-parsing `source`.
            # Only for the LIVE path — a historical prompt's follow-up
            # `.answered`/`.cancelled` hits the `_resolved_prompt_ids`
            # early-return before consulting this map.
            if run_id and terminal_entry is None:
                self._workflow_prompt_sources[prompt_id] = (
                    run_id, node_name,
                )
            # spec-017: build the run_chain and populate it on the
            # source dict so downstream consumers (renderers, replay
            # tooling) can walk the nested lineage without querying
            # the registry themselves. Then derive the parent_hint
            # from the same chain so the ApprovalBlock breadcrumb and
            # the source's `run_chain` never diverge.
            run_chain = self._build_run_chain(run_id, node_name)
            source["run_chain"] = run_chain
            # spec-014 fallback: when the registry doesn't know the
            # run (replay-only paths), emit a bare `workflow <run_id>`
            # label so the breadcrumb still anchors to something.
            if run_chain:
                parent_hint = self._format_run_chain(run_chain)
            else:
                parent_hint = self._derive_workflow_parent_hint(
                    run_id, node_name=node_name,
                )
            # Fire the durable `awaiting_approval` UI event — the
            # matching WorkflowRenderer transitions the node row to
            # `paused` on the resulting dispatch. bug-056: skipped for
            # historical prompts (terminal branch) to keep the resume
            # append count at zero (AC #4).
            if (
                self._workflows_impl is not None
                and run_id
                and terminal_entry is None
            ):
                try:
                    await self._workflows_impl.emit_awaiting_approval(
                        run_id=run_id,
                        node_name=node_name,
                    )
                except Exception:
                    _LOGGER.exception(
                        "event_adapter: emit_awaiting_approval raised "
                        "for prompt_id=%r run_id=%r",
                        prompt_id,
                        run_id,
                    )

        if terminal_entry is not None:
            kind, footer = terminal_entry
            if kind == "allowed":
                terminal: tuple[bool | None, str] | None = (True, footer)
            elif kind == "denied":
                terminal = (False, footer)
            else:
                # interrupted: match the widget's resolved-row styling
                # (Coordinator note); reuse `-denied` (see
                # `ApprovalBlock._resolved_classes`).
                terminal = (None, footer)
            try:
                await self._app.mount_approval_block(
                    rule_id_val,
                    action_val,
                    target_val,
                    parent_hint=parent_hint,
                    terminal=terminal,
                )
            except TypeError:
                # Older mock apps that don't accept the bug-056
                # `terminal=` kwarg — fall back to no-arg mount. The
                # block will render interactive there (tests using
                # such mocks can't inspect the terminal render
                # anyway).
                try:
                    await self._app.mount_approval_block(
                        rule_id_val,
                        action_val,
                        target_val,
                    )
                except Exception:
                    _LOGGER.exception(
                        "event_adapter: mount_approval_block "
                        "(terminal fallback) raised for prompt_id=%r",
                        prompt_id,
                    )
            except Exception:
                _LOGGER.exception(
                    "event_adapter: mount_approval_block (terminal) "
                    "raised for prompt_id=%r",
                    prompt_id,
                )
            # Bookkeeping: mark this prompt resolved so the
            # answered/cancelled handlers early-return + the bridge
            # write-guard triggers on any residual path.
            self._resolved_prompt_ids.add(prompt_id)
            # Do NOT spawn the bridge — the terminal block never sets
            # `_resolved_event`, so anything parked on `resolved()` would
            # hang forever. bug-056 keeps the durable log untouched by
            # simply not starting the bridge for historical prompts
            # (AC #4 primary defence).
            return

        block: Any = None
        try:
            block = await self._app.mount_approval_block(
                rule_id_val,
                action_val,
                target_val,
                parent_hint=parent_hint,
            )
        except TypeError:
            # Back-compat with mock apps that don't accept the
            # spec-014 `parent_hint` kwarg (older test doubles from
            # spec-004). Fall back to the positional-only call so
            # existing tests keep passing.
            try:
                block = await self._app.mount_approval_block(
                    rule_id_val,
                    action_val,
                    target_val,
                )
            except Exception:
                _LOGGER.exception(
                    "event_adapter: mount_approval_block raised "
                    "for prompt_id=%r",
                    prompt_id,
                )
        except Exception:
            _LOGGER.exception(
                "event_adapter: mount_approval_block raised for prompt_id=%r",
                prompt_id,
            )

        # bug-021 Symptom 2: bridge the widget's local resolution back
        # onto the store as a `WIRE_PROMPT_ANSWERED` event.
        # Pre-bug-021 no production code awaited `block.resolved()`, so
        # `PromptsImpl.choice()`'s future never resolved and the tool
        # call hung indefinitely.
        #
        # The bridge lives HERE (adapter, not `ChatApp.mount_approval_block`)
        # because the adapter owns `prompt_id` + the `session_store`
        # for the append; the app helper stays purely UI-mount
        # (Coordinator open Q resolution).
        #
        # `mount_approval_block` returns `None` in some mock-app
        # fixtures (spec-014 doubles swallow the mount) — skip the
        # bridge in that case.
        if block is not None and hasattr(block, "resolved"):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop (rare sync-test path). The bridge is
                # skipped; the durability guarantee is best-effort.
                return
            task = loop.create_task(
                self._bridge_widget_resolution(prompt_id, block),
            )
            task.add_done_callback(_log_task_exception)

    async def _bridge_widget_resolution(
        self,
        prompt_id: str,
        block: Any,
    ) -> None:
        """Await the mounted `ApprovalBlock`; emit `WIRE_PROMPT_ANSWERED`.

        bug-021 Symptom 2 fix. Fire-and-forget task spawned from
        :meth:`_handle_prompt_requested`. Awaits
        :meth:`ApprovalBlock.resolved`, maps the widget's
        ``PromptCallbackReturn`` to a response-key string via
        :func:`response_key_from`, and appends a fresh
        ``WIRE_PROMPT_ANSWERED`` event to the session store with the
        matching ``prompt_id``.

        The store append triggers the same
        :meth:`_handle_prompt_answered` branch a manually-fabricated
        answered event would — the pending
        :class:`PromptsImpl` future resolves and the awaiting
        ``choice()`` returns to the caller.

        Cancellation contract: if the widget's ``resolved()`` raises
        :class:`asyncio.CancelledError` (e.g. the app tore down while
        the user was still deciding), the bridge logs at debug and
        returns silently — the `WIRE_PROMPT_CANCELLED` path handled
        the cleanup already.

        Idempotence: relies on
        :attr:`_resolved_prompt_ids` (via
        :meth:`_handle_prompt_answered`) — a second dispatch of the
        same `prompt_id` is a no-op.
        """
        try:
            result = await block.resolved()
        except asyncio.CancelledError:
            _LOGGER.debug(
                "event_adapter: approval bridge cancelled for prompt_id=%r",
                prompt_id,
            )
            return
        except Exception:
            _LOGGER.exception(
                "event_adapter: block.resolved() raised for prompt_id=%r",
                prompt_id,
            )
            return
        # bug-056 defence-in-depth: if the widget's `_resolved_event`
        # somehow fired for a prompt whose historical resolution is
        # already durable (e.g. a residual code path outside the
        # bug-056 replay-terminal branch), don't append a duplicate
        # `WIRE_PROMPT_ANSWERED` to the store. The pre-bug-056 code
        # only checked `_resolved_prompt_ids` inside `_handle_prompt_answered`,
        # which fires AFTER the append — too late. Guarding here means
        # any future refactor that reintroduces `_resolve` on the replay
        # path still cannot corrupt the ground-truth log (AC #4).
        if prompt_id in self._resolved_prompt_ids:
            _LOGGER.debug(
                "event_adapter: approval bridge skipped (already resolved) "
                "for prompt_id=%r",
                prompt_id,
            )
            return

        try:
            # Local import to avoid a circular src/cli/chat/widgets ↔
            # src/cli/chat/event_adapter cycle at module load time.
            from cli.chat.widgets.approval_block import (
                response_key_from,
            )
            response = response_key_from(result)
        except Exception:
            _LOGGER.exception(
                "event_adapter: response_key_from failed for prompt_id=%r",
                prompt_id,
            )
            return

        # bug-025 / closes bug-022: serialise the (effect, Rule) tuple
        # so the answered wire event carries the Rule dict alongside
        # the response key. The callback side
        # (`InlineApprovalCallback._translate_response`) rebuilds the
        # tuple and hands it back to
        # `src/providers/authz.py:1106`'s persist branch. Fast paths
        # (bare `AuthzEffect`) omit the `rule` field so the wire
        # payload stays byte-identical to the pre-bug-025 shape —
        # WIRE_PROMPT_ANSWERED gains an OPTIONAL field only (D-007,
        # no new wire kinds).
        rule_dict: dict[str, Any] | None = None
        if isinstance(result, tuple):
            _effect, _rule = result
            try:
                rule_dict = _rule.model_dump(mode="json", by_alias=True)
            except Exception:
                _LOGGER.exception(
                    "event_adapter: Rule.model_dump failed for prompt_id=%r",
                    prompt_id,
                )
                # A serialisation failure is a bug in Rule shape, not
                # the flow — proceed with rule_dict=None so at least
                # the response key still lands (fast-path degradation).
                rule_dict = None

        if (
            self._session_store is None
            or self._session_id is None
            or self._thread_id is None
        ):
            # Headless / partial-mount test paths: no store to append
            # to. The bridge is a durability + composition seam; the
            # widget's `_resolved_event` still fires so any direct
            # awaiter (widget-level tests) unblocks.
            _LOGGER.debug(
                "event_adapter: approval bridge skipped (no store) "
                "for prompt_id=%r",
                prompt_id,
            )
            return

        answered_payload: dict[str, Any] = {
            "prompt_id": prompt_id,
            "response": response,
        }
        # bug-025: OPTIONAL rule field — only present when the widget
        # resolved via `(AuthzEffect, Rule)`. Backward-compat: consumers
        # that read only `prompt_id` + `response` are unchanged.
        if rule_dict is not None:
            answered_payload["rule"] = rule_dict

        answered = Event(
            event_id=EventId(f"ev_{uuid4()}"),
            parent_event_id=None,
            thread_id=self._thread_id,
            session_id=self._session_id,
            type=EventType.UI_EVENT,
            data={
                "schema_version": 1,
                "kind": WIRE_PROMPT_ANSWERED,
                "payload": answered_payload,
            },
            actor=Actor(kind="system"),
            created_at=datetime.now(UTC),
        )
        try:
            await self._session_store.append_event(self._thread_id, answered)
        except Exception:
            _LOGGER.exception(
                "event_adapter: append_event(WIRE_PROMPT_ANSWERED) failed "
                "for prompt_id=%r",
                prompt_id,
            )
            return

        # Publish to the runtime bus so the live UI_EVENT subscription
        # (registered in `ChatApp.on_mount` per bug-020) delivers the
        # answered event to `event_adapter.handle` in the SAME session.
        # Without this the store durability is fine but
        # `PromptsImpl.choice()`'s future only resolves on next-boot
        # replay — which is Symptom 2 verbatim. Mirrors
        # `PromptsImpl._append_ui_event`'s append-then-publish shape;
        # bus failures are swallowed (durability is the store append,
        # delivery is best-effort).
        bus = getattr(
            getattr(self._app, "session", None), "runtime", None,
        )
        bus = getattr(bus, "bus", None) if bus is not None else None
        if bus is not None and hasattr(bus, "publish"):
            with contextlib.suppress(Exception):
                await bus.publish(answered)

    def _derive_workflow_parent_hint(
        self,
        run_id: str,
        *,
        node_name: str = "",
    ) -> str:
        """Build the ↳ parent hint string for a workflow-scoped prompt.

        spec-014 baseline: uses the workflow record's ``ref`` when
        available (registry hit); falls back to the bare ``run_id``
        when the registry doesn't know the run (replay-only paths).

        spec-017: when the innermost prompt-source run has a
        non-None ``parent_run_id``, this walks the chain via
        :meth:`_build_run_chain` and returns the formatted breadcrumb
        ``workflow A.ref → B.ref · node <node_name>``. A single-entry
        chain (top-level workflow, no nesting) collapses to
        ``workflow <ref>`` — the same shape spec-014 shipped so
        existing renderers stay stable.
        """
        chain = self._build_run_chain(run_id, node_name)
        if chain:
            return self._format_run_chain(chain)
        # No registry hit — fall back to the bare run_id label.
        return f"workflow {run_id}"

    def _build_run_chain(
        self,
        innermost_run_id: str,
        innermost_node_name: str,
    ) -> list[dict[str, str]]:
        """Walk ``parent_run_id`` up; return the chain outermost-first.

        spec-017 AC #9-10. Returned entries have the shape
        ``{"run_id": str, "ref": str, "node_name": str}``; the innermost
        entry carries the ``node_name`` supplied by the caller (the
        source's ``node_name``), while ancestor entries carry
        ``node_name=""`` (they didn't fire the prompt themselves).

        Returns an empty list when the ``innermost_run_id`` is unknown
        to the registry — the caller can then fall back to a bare
        label without pretending to know the chain.
        """
        if self._workflows_impl is None:
            return []
        registry = getattr(self._workflows_impl, "_workflow_registry", None)
        if not isinstance(registry, dict):
            return []

        # `_workflow_registry` keys are `WorkflowRunId` (a `str`
        # subclass). Build a plain-str lookup once so the walk stays
        # O(chain_depth) rather than O(chain_depth * registry_size).
        by_str: dict[str, Any] = {
            str(key): record for key, record in registry.items()
        }

        chain: list[dict[str, str]] = []
        cur: str | None = innermost_run_id
        seen: set[str] = set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            rec = by_str.get(cur)
            if rec is None:
                break
            ref = getattr(rec, "ref", None)
            ref_str = ref if isinstance(ref, str) else ""
            chain.append({
                "run_id": cur,
                "ref": ref_str,
                "node_name": (
                    innermost_node_name if cur == innermost_run_id else ""
                ),
            })
            parent = getattr(rec, "parent_run_id", None)
            cur = str(parent) if parent is not None else None

        return list(reversed(chain))

    @staticmethod
    def _format_run_chain(run_chain: list[dict[str, str]]) -> str:
        """Format a run_chain as ``workflow A.ref → B.ref · node X``.

        spec-017 AC #11: single-entry chain renders as
        ``workflow <ref>`` (no arrow, no trailing node clause when
        the node name is empty) so existing single-workflow renderers
        stay stable with spec-014's shipped shape.
        """
        if not run_chain:
            return ""
        refs = " → ".join(
            entry.get("ref") or entry.get("run_id", "")
            for entry in run_chain
        )
        innermost = run_chain[-1]
        node_name = innermost.get("node_name") or ""
        if node_name:
            return f"workflow {refs} · node {node_name}"
        return f"workflow {refs}"

    def _handle_prompt_answered(self, payload: dict[str, Any]) -> None:
        """Resolve the in-flight `PromptsImpl` future on `.answered`.

        Idempotent per `prompt_id`; a second dispatch is a no-op
        (setting a done future's result would raise
        `InvalidStateError`).

        spec-014: if the answered prompt was workflow-scoped
        (tracked in `_workflow_prompt_sources` from the matching
        `_handle_prompt_requested`), fire
        `WorkflowsImpl.emit_approval_resolved` so the matching
        `WorkflowNodeRow` transitions back to `running` via the
        renderer's dispatch of the resulting UI event.
        """
        prompt_id = payload.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            return
        if prompt_id in self._resolved_prompt_ids:
            return
        self._resolved_prompt_ids.add(prompt_id)
        response = payload.get("response", payload.get("answer", ""))
        if not isinstance(response, str):
            response = str(response)
        # bug-025: forward the optional `rule` dict through to
        # `PromptsImpl.resolve_pending` so the awaiting `choice()`
        # future returns a `PromptResponse` carrying the widget-built
        # Rule. Missing `rule` (backward-compat + fast-path `y`/`n`) is
        # the pre-bug-025 shape byte-identical.
        rule_payload = payload.get("rule")
        rule_dict: dict[str, Any] | None = (
            rule_payload if isinstance(rule_payload, dict) else None
        )
        if self._prompts_impl is not None:
            self._prompts_impl.resolve_pending(
                prompt_id, response, rule=rule_dict,
            )
        # spec-014: workflow branch — schedule the durable
        # `approval_resolved` emit. Fire-and-forget so the sync
        # handler stays sync; the awaiting `choice()` unblocks via
        # the resolve above.
        self._maybe_emit_approval_resolved(prompt_id, response)
        # bug-056: recompute the composer gate. Belt + braces with the
        # live-path `InlineApprovalCallback.finally → clear_awaiting_approval`
        # so headless / synthetic-event tests without a callback still
        # un-freeze once every mounted block reports `is_resolved` (AC #10).
        recompute = getattr(self._app, "recompute_awaiting_approval", None)
        if callable(recompute):
            recompute()

    def _handle_prompt_cancelled(self, payload: dict[str, Any]) -> None:
        """Cancel the in-flight `PromptsImpl` future on `.cancelled`.

        Raises `asyncio.CancelledError` from the awaiting `choice()`
        / `text()` — Coordinator-locked behaviour matching the Esc
        → deny-once semantics of the shipped approval UX. Idempotent
        per `prompt_id`.

        spec-014: same workflow-source branch as
        `_handle_prompt_answered`; the emitted `.approval_resolved`
        payload's `resolution` is `"cancelled"`.
        """
        prompt_id = payload.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            return
        if prompt_id in self._resolved_prompt_ids:
            return
        self._resolved_prompt_ids.add(prompt_id)
        if self._prompts_impl is not None:
            self._prompts_impl.cancel_pending(prompt_id)
        self._maybe_emit_approval_resolved(prompt_id, "cancelled")
        # bug-056: same recompute nudge as the answered path.
        recompute = getattr(self._app, "recompute_awaiting_approval", None)
        if callable(recompute):
            recompute()

    def _maybe_emit_approval_resolved(
        self,
        prompt_id: str,
        response: str,
    ) -> None:
        """Fire the durable `approval_resolved` UI event, if workflow-scoped.

        spec-014: reads `_workflow_prompt_sources` for the matching
        `(run_id, node_name)` tuple. Deletes the entry to avoid a
        second emit on a duplicate `.answered` dispatch (belt +
        braces with `_resolved_prompt_ids`). Schedules the async
        emit as a fire-and-forget task so the sync handler surface
        stays sync.

        `response` becomes the payload's `resolution` field:
        `"y"`/`"a"` map to `"approved"`, `"n"`/`"d"` map to
        `"denied"`, `"cancelled"` (from `_handle_prompt_cancelled`)
        passes through verbatim.
        """
        entry = self._workflow_prompt_sources.pop(prompt_id, None)
        if entry is None:
            return
        if self._workflows_impl is None:
            return
        run_id, node_name = entry
        resolution = _response_to_resolution(response)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (rare: sync test path). Skip; the
            # durability guarantee is best-effort in that regime.
            return
        coro = self._workflows_impl.emit_approval_resolved(
            run_id=run_id,
            node_name=node_name,
            resolution=resolution,
        )
        task = loop.create_task(coro)
        # Track the task so `aclose`-style teardowns can await it if
        # needed; a bare fire-and-forget would drop the reference and
        # potentially GC-cancel the coroutine on Python 3.11+.
        task.add_done_callback(_log_task_exception)

    # ------------------------------------------------------------------
    # spec-006: card dispatch handlers
    # ------------------------------------------------------------------

    def _get_card_id(self, payload: dict[str, Any]) -> CardId | None:
        """Extract + validate `card_id` from a card event's payload.

        Returns `None` (silent no-op) when the payload is malformed —
        replay of a mis-shaped future JSONL row shouldn't crash the
        adapter.
        """
        raw = payload.get("card_id")
        if not isinstance(raw, str) or not raw:
            return None
        try:
            return CardId(raw)
        except (ValueError, TypeError):
            return None

    async def _handle_card_opened(
        self,
        event: Event,
        payload: dict[str, Any],
    ) -> None:
        """Mount a `CardWidget` for a fresh `kolsch.card.opened`.

        Idempotent per `card_id`: if the card is already tracked
        (replay + live path racing, or a duplicate emission) return
        silently — no second widget mounted.

        spec-006 shipped a `_CardMountStub` here; spec-007 swaps in
        `CardWidget` behind the `CardMountState` Protocol seam.
        Handler code below (`_handle_card_appended`,
        `_handle_card_set`, `_handle_card_status`, `_handle_card_closed`,
        `_handle_card_snapshot`) is unchanged — every mutation still
        goes through the Protocol methods
        (`apply_append` / `apply_snapshot` / `apply_status` /
        `apply_close`) which `CardWidget` implements identically.

        Falls back to `_CardMountStub` when the app has no
        `#transcript` container (headless service tests never mount
        an app; the stub's in-memory state suffices for the
        adapter's idempotence checks).
        """
        _ = event
        card_id = self._get_card_id(payload)
        if card_id is None:
            return
        if card_id in self._card_widgets:
            return
        raw_kind = payload.get("kind")
        kind = raw_kind if isinstance(raw_kind, str) else "markdown"
        raw_title = payload.get("title")
        title = raw_title if isinstance(raw_title, str) else ""

        # spec-007: try to mount a `CardWidget` in the transcript.
        # Headless tests without a live `#transcript` fall back to
        # the spec-006 in-memory stub so idempotence + LRU tests
        # still exercise the Protocol seam without booting Textual.
        entry: CardMountState
        transcript = self._locate_transcript()
        if transcript is not None:
            widget = self._try_build_card_widget(card_id, kind, title)
            if widget is not None:
                entry = widget
                self._card_widgets[card_id] = entry
                await self._mount_card_widget(widget, transcript)
                # spec-017: workflow-kind cards whose bound run
                # already has a `parent_run_id` populated (i.e.,
                # `Workflows.start()` fired the card-open BEFORE
                # this dispatch reached us) get their header's
                # `↳ from <parent-ref>` hint applied immediately.
                # The common flow is card-open first, `start()`
                # second — the fan-out in `_handle_workflow_progress`
                # handles that path via `_refresh_workflow_parent_ref`.
                if kind == "workflow":
                    self._apply_workflow_parent_ref_from_card(
                        card_id, widget,
                    )
                return
        # Fallback: no transcript container available (headless
        # service tests) — track in memory via the spec-006 stub.
        entry = _CardMountStub(card_id, kind)
        self._card_widgets[card_id] = entry

    def _apply_workflow_parent_ref_from_card(
        self,
        card_id: CardId,
        widget: Any,
    ) -> None:
        """Push the header ``↳ from <parent-ref>`` hint at card-open time.

        spec-017 AC #8. Called immediately after a workflow-kind
        `CardWidget` mounts; iterates
        `WorkflowsImpl._workflow_registry` for a record bound to this
        card_id whose `parent_run_id` is populated. When found, looks
        up the parent's `ref` and calls the renderer's
        `set_parent_ref`. Silent no-op when:

        - `WorkflowsImpl` isn't wired (headless tests).
        - No record is bound to this card yet (the common flow — the
          `start()` call that binds the card follows the card-open
          dispatch; the fan-out in `_handle_workflow_progress` then
          applies the hint via `_refresh_workflow_parent_ref`).
        - The record has no `parent_run_id` (top-level workflow).
        - The parent's record isn't in the registry (already
          evicted; the fallback bare-label path).
        """
        if self._workflows_impl is None:
            return
        registry = getattr(self._workflows_impl, "_workflow_registry", None)
        if not isinstance(registry, dict):
            return
        body = getattr(widget, "_body", None)
        if body is None or not hasattr(body, "set_parent_ref"):
            return
        card_id_str = str(card_id)
        for record in registry.values():
            bound_card = getattr(record, "card", None)
            bound_card_id = getattr(bound_card, "card_id", None)
            if bound_card_id is None or str(bound_card_id) != card_id_str:
                continue
            parent_run_id = getattr(record, "parent_run_id", None)
            if parent_run_id is None:
                return
            parent_ref: str | None = None
            for candidate_key, candidate in registry.items():
                if str(candidate_key) == str(parent_run_id):
                    ref = getattr(candidate, "ref", None)
                    if isinstance(ref, str) and ref:
                        parent_ref = ref
                    break
            if parent_ref is None:
                return
            with contextlib.suppress(Exception):
                body.set_parent_ref(parent_ref)
            return

    def _locate_transcript(self) -> Any | None:
        """Return the `#transcript` container, or `None` if absent.

        Headless service tests use a stub app with no compose tree;
        `query_one` raises. We treat that as "widget mount not
        available" so the adapter falls back to the spec-006 stub
        seamlessly.
        """
        try:
            return self._app.query_one("#transcript")
        except Exception:  # noqa: BLE001 — headless / partial mount
            return None

    def _try_build_card_widget(
        self,
        card_id: CardId,
        kind: str,
        title: str,
    ) -> Any | None:
        """Construct a `CardWidget` if the app supports widget mounts.

        Returns `None` when import or construction fails (headless
        tests without Textual mounted, or an unknown kind). The
        caller falls back to `_CardMountStub` in that case.
        """
        try:
            from cli.chat.widgets.card_widget import CardWidget
        except Exception:  # noqa: BLE001 — widget layer optional
            return None
        try:
            return CardWidget(card_id=card_id, kind=kind, title=title)
        except Exception:
            _LOGGER.exception(
                "event_adapter: CardWidget construction failed for %r",
                card_id,
            )
            return None

    async def _mount_card_widget(
        self,
        widget: Any,
        transcript: Any,
    ) -> None:
        """Mount `widget` in `transcript` — best-effort.

        A mount failure is logged and swallowed so the adapter's
        tracking state stays consistent with the spec-006 stub.
        """
        try:
            await transcript.mount(widget)
        except Exception:
            _LOGGER.exception(
                "event_adapter: CardWidget mount raised for %r",
                getattr(widget, "card_id", "<unknown>"),
            )
            return
        # Scroll to the newly-mounted card. Best-effort — a missing
        # `scroll_end` (non-VerticalScroll transcript) is fine.
        try:
            transcript.scroll_end(animate=False)
        except Exception:  # noqa: BLE001 — some containers don't scroll
            pass

    def _handle_card_appended(
        self,
        event: Event,
        payload: dict[str, Any],
    ) -> None:
        """Route an `appended` chunk through the CardMountState Protocol.

        Idempotent via the entry's bounded LRU on `event_id` (maxsize
        1000 per card). If the `opened` event has not applied yet
        (out-of-order replay), the append is silently dropped —
        `opened` is critical-lane so this shouldn't happen in practice.
        """
        card_id = self._get_card_id(payload)
        if card_id is None:
            return
        entry = self._card_widgets.get(card_id)
        if entry is None:
            return
        chunk = payload.get("chunk")
        if not isinstance(chunk, str):
            return
        # Protocol call — writes through the CardMountState seam.
        entry.apply_append(str(event.event_id), chunk)

    def _handle_card_set(
        self,
        event: Event,
        payload: dict[str, Any],
    ) -> None:
        """Route a `set` (content replace) — last-writer-wins.

        Direct `.content` write preserves spec-006's stub contract; a
        widget-backed impl also forwards the payload into
        `apply_event` so the body renderer refreshes.
        """
        card_id = self._get_card_id(payload)
        if card_id is None:
            return
        entry = self._card_widgets.get(card_id)
        if entry is None:
            return
        content = payload.get("content")
        if isinstance(content, str):
            # `set` replaces via the same Protocol seam as snapshot.
            entry.content = content
        # spec-007: forward to the widget's `apply_event` so the body
        # renderer refreshes. Stub impls have no `apply_event` — the
        # `getattr` check keeps the spec-006 handler UNCHANGED for
        # them.
        if hasattr(entry, "apply_event"):
            entry.apply_event(_UIEventDataView(event, payload))

    def _handle_card_set_progress(
        self,
        event: Event,
        payload: dict[str, Any],
    ) -> None:
        """Route a `set_progress` — forwards to the widget's `apply_event`.

        No-op on the spec-006 stub (which has no progress state);
        the widget-backed impl updates the `ProgressBar` via its
        body renderer.
        """
        card_id = self._get_card_id(payload)
        if card_id is None:
            return
        entry = self._card_widgets.get(card_id)
        if entry is None:
            return
        if hasattr(entry, "apply_event"):
            entry.apply_event(_UIEventDataView(event, payload))

    def _handle_card_status(
        self,
        event: Event,
        payload: dict[str, Any],
    ) -> None:
        """Route a `status` update — last-writer-wins on `state`."""
        _ = event
        card_id = self._get_card_id(payload)
        if card_id is None:
            return
        entry = self._card_widgets.get(card_id)
        if entry is None:
            return
        status = payload.get("status")
        if isinstance(status, str):
            entry.apply_status(status)

    def _handle_card_closed(
        self,
        event: Event,
        payload: dict[str, Any],
    ) -> None:
        """Route a terminal transition through the Protocol.

        Idempotent — a card whose state is already terminal
        (`success` / `error` / `cancelled`) stays put.
        """
        _ = event
        card_id = self._get_card_id(payload)
        if card_id is None:
            return
        entry = self._card_widgets.get(card_id)
        if entry is None:
            return
        raw_final = payload.get("final_status")
        final_status = raw_final if isinstance(raw_final, str) else "success"
        raw_msg = payload.get("error_message")
        error_message = raw_msg if isinstance(raw_msg, str) else None
        entry.apply_close(final_status, error_message)

    def _handle_card_snapshot(
        self,
        event: Event,
        payload: dict[str, Any],
    ) -> None:
        """Apply a snapshot — replace content; clear applied_appends.

        Idempotent via the entry's bounded LRU on `event_id` (maxsize
        100 per card). Snapshot replay is inline (Coordinator lock);
        the standalone helper deferred to spec-007.
        """
        card_id = self._get_card_id(payload)
        if card_id is None:
            return
        entry = self._card_widgets.get(card_id)
        if entry is None:
            return
        content = payload.get("content")
        if not isinstance(content, str):
            return
        entry.apply_snapshot(str(event.event_id), content)

    def _handle_card_notice(
        self,
        event: Event,
        payload: dict[str, Any],
    ) -> None:
        """Route a ``kolsch.card.notice`` event to the right mount.

        Two payload shapes, both keyed by ``notice_id``:

        - Payload with ``card_id`` — mount inside the target card's
          [`NoticeSlot`][cli.chat.widgets.notice_slot.NoticeSlot] via
          ``CardWidget.mount_notice``. Silent no-op when the card
          isn't tracked (out-of-order dispatch / replay of a notice
          for a card that never mounted).
        - Payload without ``card_id`` — mount a
          [`NoticeWidget`][cli.chat.widgets.notice.NoticeWidget] at
          the transcript root.

        Repeat dispatch with the same ``notice_id`` (drop-notice
        count increment) calls ``update_text`` on the tracked widget
        instead of mounting a second widget. Idempotent per
        ``notice_id`` via ``_applied_notice_ids`` bounded LRU; the
        widget handle for update-in-place lives in
        ``_notice_widgets``.
        """
        raw_id = payload.get("notice_id")
        if not isinstance(raw_id, str) or not raw_id:
            # Legacy notice with no id — dedupe on `event.event_id`.
            raw_id = str(event.event_id)

        text = payload.get("text")
        text_str = text if isinstance(text, str) else ""
        raw_kind = payload.get("kind")
        kind_str = raw_kind if isinstance(raw_kind, str) else "info"

        if raw_id in self._applied_notice_ids:
            # Update-in-place: same notice_id, new text.
            existing = self._notice_widgets.get(raw_id)
            if existing is not None:
                update = getattr(existing, "update_text", None)
                if callable(update):
                    with contextlib.suppress(Exception):
                        update(text_str)
            return
        self._applied_notice_ids.add(raw_id)

        card_id_str = payload.get("card_id")
        if isinstance(card_id_str, str) and card_id_str:
            self._mount_in_card_notice(
                raw_id, card_id_str, text_str, kind_str,
            )
            return

        # Transcript-root notice — schedule the async mount.
        # `_handle_card_notice` is sync (called from
        # ``_handle_ui_event``); schedule the mount as a task.
        asyncio.create_task(
            self._mount_root_notice(raw_id, text_str, kind_str),
        )

    def _mount_in_card_notice(
        self,
        notice_id: str,
        card_id_str: str,
        text: str,
        kind: str,
    ) -> None:
        """Mount a notice inside a specific card's ``NoticeSlot``.

        Called by ``_handle_card_notice`` when the payload carries a
        ``card_id``. Silent no-op when the card isn't in
        ``_card_widgets`` (unmounted / unknown — the notice is
        dropped by design).
        """
        try:
            card_id = CardId(card_id_str)
        except (ValueError, TypeError):
            return
        entry = self._card_widgets.get(card_id)
        if entry is None:
            return
        mount = getattr(entry, "mount_notice", None)
        if not callable(mount):
            return
        try:
            widget = mount(text, kind)
        except Exception:
            _LOGGER.exception(
                "event_adapter: mount_notice raised for card_id=%r",
                card_id_str,
            )
            return
        if widget is not None:
            self._notice_widgets[notice_id] = widget

    def _handle_workflow_progress(
        self,
        event: Event,
        payload: dict[str, Any],
    ) -> None:
        """Route a `kolsch.workflow.progress` event to the workflow card body.

        spec-013 §Renderer wiring. The `WorkflowsImpl` bridge drain
        emits one of these per executor `WorkflowEvent`. The event
        carries no `card_id` (workflows_impl doesn't know which card
        is bound at emission time), so this handler fans out to every
        tracked `WorkflowRenderer` and lets each renderer decide via
        `run_id` whether the payload is theirs — a
        cardinality-preserving broadcast that scales fine at M5's
        one-workflow-at-a-time cadence.

        Also wires the renderer to this adapter on first sight — the
        renderer's `_live_nodes_by_trace_and_invocation` registration
        happens on `NODE_STARTED` and depends on `register_adapter`
        having been called.
        """
        _ = event
        # Locate every mounted workflow-kind CardWidget's body
        # renderer and forward the payload. In the common one-live-
        # workflow case this fans out to a single renderer.
        target_run_id = payload.get("run_id")
        for entry in self._card_widgets.values():
            body = getattr(entry, "_body", None)
            if body is None:
                continue
            # Duck-type on the presence of `_node_rows` (a
            # WorkflowRenderer-only attribute) so we don't
            # `isinstance`-couple the adapter to the widget class.
            if not hasattr(body, "_node_rows"):
                continue
            # Register the adapter on first sight — the renderer's
            # `register_adapter` is idempotent.
            with contextlib.suppress(Exception):
                body.register_adapter(self)
            # Filter by run_id when the renderer already has one
            # anchored (post-`RUN_STARTED`); pre-anchor, forward all
            # payloads so the anchor lands.
            renderer_run_id = getattr(body, "_run_id", None)
            if (
                renderer_run_id is not None
                and target_run_id is not None
                and renderer_run_id != target_run_id
            ):
                continue
            # spec-017: if this run has a `parent_run_id` and the
            # renderer's `_parent_ref` isn't already set, refresh the
            # header hint with the parent's `ref`. Idempotent —
            # `set_parent_ref` no-ops when the value is unchanged.
            if isinstance(target_run_id, str):
                self._refresh_workflow_parent_ref(body, target_run_id)
            data_view = _UIEventDataView(event, payload)
            with contextlib.suppress(Exception):
                body.apply_event(data_view)

    async def _dispatch_sub_agent_ui_event(
        self,
        event: Event,
        payload: dict[str, Any],
    ) -> None:
        """bug-036d: unwrap a sub-agent UI envelope and dispatch async.

        Fires from :meth:`_handle_ui_event`'s sub-agent branch. The
        outer envelope carries the ``event_type`` string, the
        ``trace_id``, ``invocation_id``, ``event_id`` and the inner
        ``payload`` dict (session-event data). We reconstruct a
        lightweight :class:`_ReconstructedSubAgentEvent`-shaped
        object and forward it to :meth:`dispatch_sub_agent_event`
        so the standard handler chain runs.

        Also runs the renderer fan-out for pre-036d compatibility —
        the renderer's ``register_adapter`` bookkeeping still needs
        to fire so the ``(trace_id, invocation_id)`` map is populated.
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
        try:
            event_type = EventType(event_type_raw)
        except ValueError:
            return
        raw_data = payload.get("payload")
        data = dict(raw_data) if isinstance(raw_data, dict) else {}
        # bug-033: the composite key lookup wants ``invocation_id``
        # on the inner event data. Stamp it here so
        # ``_route_to_workflow_node`` finds the right row.
        if "invocation_id" not in data:
            data["invocation_id"] = invocation_id
        wire_event_id = payload.get("event_id")
        # Reconstruct via a duck-typed object with the fields the
        # adapter's handlers + ``_route_to_workflow_node`` read.
        # Prefer the outer envelope's ``created_at`` so downstream
        # timestamp rendering (``UserChatbox`` header) stays stable
        # across replays.
        from cli.chat.widgets.card_body.workflow import (  # noqa: PLC0415
            _ReconstructedSubAgentEvent,
        )
        outer_ts = _now_ts(event)
        inner = _ReconstructedSubAgentEvent(
            type=event_type,
            data=data,
            created_at=outer_ts,
            trace_id=trace_id,
            event_id=(
                wire_event_id if isinstance(wire_event_id, str) else None
            ),
        )
        # Register adapter with any tracked renderers so the map
        # stays populated. This mirrors the pre-036d fan-out
        # without dispatching through the renderer.
        for entry in self._card_widgets.values():
            body = getattr(entry, "_body", None)
            if body is None or not hasattr(body, "_node_rows"):
                continue
            with contextlib.suppress(Exception):
                body.register_adapter(self)
        # bug-036d: async dispatch through the standard handler
        # chain. The trace_id safety check inside
        # ``dispatch_sub_agent_event`` is a no-op here (outer +
        # inner trace_id are identical by construction).
        await self.dispatch_sub_agent_event(
            inner, outer_trace_id=trace_id,
        )

    def _refresh_workflow_parent_ref(
        self,
        body: Any,
        run_id: str,
    ) -> None:
        """Push the parent workflow's ``ref`` onto the renderer's header.

        spec-017 AC #7-8. No-op when the run is top-level (no
        ``parent_run_id``) or the parent's record isn't in the
        registry (replay-only path). Silently drops any renderer
        without a ``set_parent_ref`` seam so headless / test doubles
        stay compatible.
        """
        if self._workflows_impl is None:
            return
        registry = getattr(self._workflows_impl, "_workflow_registry", None)
        if not isinstance(registry, dict):
            return
        # `_workflow_registry` keys are `WorkflowRunId` (a `str`
        # subclass). Find the record by str-equality.
        rec = None
        for key, candidate in registry.items():
            if str(key) == run_id:
                rec = candidate
                break
        if rec is None:
            return
        parent_run_id = getattr(rec, "parent_run_id", None)
        if parent_run_id is None:
            return
        parent_rec = None
        for key, candidate in registry.items():
            if str(key) == str(parent_run_id):
                parent_rec = candidate
                break
        if parent_rec is None:
            return
        parent_ref = getattr(parent_rec, "ref", None)
        if not isinstance(parent_ref, str) or not parent_ref:
            return
        # Only refresh when the value changes so we don't churn the
        # widget on every progress event.
        current = getattr(body, "_parent_ref", None)
        if current == parent_ref:
            return
        set_parent_ref = getattr(body, "set_parent_ref", None)
        if callable(set_parent_ref):
            with contextlib.suppress(Exception):
                set_parent_ref(parent_ref)

    def _handle_workflow_node_awaiting_approval(
        self,
        event: Event,
        payload: dict[str, Any],
    ) -> None:
        """Route a `kolsch.workflow.node.awaiting_approval` to the renderer.

        spec-014 §Renderer wiring. Fans out to every mounted
        `WorkflowRenderer` (same cardinality-preserving pattern as
        `_handle_workflow_progress`); each renderer filters on its own
        anchored `run_id`. Sub-widget transition to `paused` (glyph
        `⚠`) happens on the renderer's `_on_awaiting_approval` call.
        """
        self._route_workflow_node_approval_event(
            event,
            payload,
            kind_label="awaiting_approval",
        )

    def _handle_workflow_node_approval_resolved(
        self,
        event: Event,
        payload: dict[str, Any],
    ) -> None:
        """Route a `kolsch.workflow.node.approval_resolved` to the renderer.

        spec-014 §Renderer wiring. Same fan-out pattern as
        `_handle_workflow_node_awaiting_approval`; each renderer
        transitions its matching row back to `running` (glyph `▸`).
        """
        self._route_workflow_node_approval_event(
            event,
            payload,
            kind_label="approval_resolved",
        )

    def _route_workflow_node_approval_event(
        self,
        event: Event,
        payload: dict[str, Any],
        *,
        kind_label: str,
    ) -> None:
        """Shared fan-out for the two spec-014 approval-lifecycle events.

        Payload carries `run_id` + `node_name`; renderers filter on
        their anchored `_run_id` when set.
        """
        _ = event
        _ = kind_label
        target_run_id = payload.get("run_id")
        for entry in self._card_widgets.values():
            body = getattr(entry, "_body", None)
            if body is None:
                continue
            if not hasattr(body, "_node_rows"):
                continue
            renderer_run_id = getattr(body, "_run_id", None)
            if (
                renderer_run_id is not None
                and target_run_id is not None
                and renderer_run_id != target_run_id
            ):
                continue
            data_view = _UIEventDataView(event, payload)
            with contextlib.suppress(Exception):
                body.apply_event(data_view)

    def _scroll_to_workflow_card(self, run_id: str) -> bool:
        """Scroll the transcript to the workflow card bound to ``run_id``.

        feat-018 AC #8/#9. Iterates ``_card_widgets`` for a
        ``workflow``-kind entry whose bound ``WorkflowsImpl`` record
        matches ``run_id``, then calls
        ``transcript.scroll_to_widget(card_widget)`` on the located
        widget. Returns ``True`` on a successful scroll, ``False`` on
        any lookup miss (unknown run_id, card scrolled off / not
        widget-backed, transcript absent). Misses log a warning and
        do nothing else — the caller (an anchor button click) treats
        the miss as a no-op.
        """
        card_widget = self._find_workflow_card_widget(run_id)
        if card_widget is None:
            _LOGGER.warning(
                "event_adapter: no live workflow card for run_id=%r; "
                "anchor scroll no-op",
                run_id,
            )
            return False
        transcript = self._locate_transcript()
        if transcript is None:
            _LOGGER.warning(
                "event_adapter: transcript absent; anchor scroll no-op "
                "for run_id=%r",
                run_id,
            )
            return False
        try:
            transcript.scroll_to_widget(card_widget, animate=False)
        except Exception:
            _LOGGER.exception(
                "event_adapter: scroll_to_widget raised for run_id=%r",
                run_id,
            )
            return False
        return True

    def _find_workflow_card_widget(self, run_id: str) -> Any | None:
        """Return the ``CardWidget`` bound to ``run_id``, or ``None``.

        Lookup path: walk :attr:`WorkflowsImpl._workflow_registry` for
        a record with matching ``run_id`` whose ``card`` field points
        at a live entry in :attr:`_card_widgets`. A run whose card was
        never widget-backed (headless stub) OR whose card_id is no
        longer tracked (scrolled off, evicted) returns ``None``.
        """
        if self._workflows_impl is None:
            return None
        registry = getattr(self._workflows_impl, "_workflow_registry", None)
        if not isinstance(registry, dict):
            return None
        target = str(run_id)
        for record_run_id, record in registry.items():
            if str(record_run_id) != target:
                continue
            card = getattr(record, "card", None)
            if card is None:
                return None
            card_id = getattr(card, "id", None) or getattr(card, "card_id", None)
            if card_id is None:
                return None
            entry = self._card_widgets.get(card_id)
            # Only widget-backed entries scroll — the spec-006
            # `_CardMountStub` is an in-memory tracker and can't be
            # scrolled to.
            if entry is None:
                return None
            if not hasattr(entry, "scroll_visible"):
                # Anything that isn't a Textual widget can't be a
                # scroll target — fall through to the None miss.
                return None
            return entry
        return None

    async def _mount_root_notice(
        self,
        notice_id: str,
        text: str,
        kind: str,
    ) -> None:
        """Mount a
        [`NoticeWidget`][cli.chat.widgets.notice.NoticeWidget] at the
        transcript root.

        Falls back silently when ``#transcript`` isn't available
        (headless service tests). The widget handle is tracked under
        ``notice_id`` so a repeat dispatch's update-in-place path
        can replace the text.
        """
        transcript = self._locate_transcript()
        if transcript is None:
            return
        try:
            from cli.chat.widgets.notice import NoticeWidget  # noqa: PLC0415
        except Exception:
            _LOGGER.exception("event_adapter: NoticeWidget import failed")
            return
        try:
            widget = NoticeWidget(text=text, kind=kind)
            await transcript.mount(widget)
        except Exception:
            _LOGGER.exception(
                "event_adapter: root notice mount failed for notice_id=%r",
                notice_id,
            )
            return
        self._notice_widgets[notice_id] = widget


__all__ = ["CardMountState", "CardTrackingEntry", "EventAdapter"]
