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
)

if TYPE_CHECKING:
    from cli.chat.app import ChatApp
    from cli.chat.services.prompts_impl import PromptsImpl
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
    ) -> None:
        self._app = app
        # tool_call_id → arguments dict (captured on TOOL_CALLED so
        # TOOL_RESULT / TOOL_FAILED can render the full signature).
        self._tool_call_args: dict[str, dict[str, Any]] = {}
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

        # spec-006/007: per-card tracking for the `_handle_card_*`
        # handlers. `_card_widgets` keys are `CardId`; values are
        # `CardMountState` Protocol impls — spec-006 shipped
        # `_CardMountStub`; spec-007 promotes to `CardWidget`
        # (widget-backed) via the Protocol seam. Handler code below
        # is UNCHANGED from spec-006 apart from `_handle_card_opened`,
        # which now also mounts the widget in the transcript.
        # `_applied_notice_ids` dedupes `kolsch.card.notice` events.
        self._card_widgets: dict[CardId, CardMountState] = {}
        self._applied_notice_ids: _BoundedLRUSet = _BoundedLRUSet(
            maxsize=_CARD_NOTICE_LRU_MAXSIZE,
        )

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

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @classmethod
    def handles(cls, event_type: EventType) -> bool:
        """True if a handler is registered for ``event_type``."""
        return event_type in cls._DISPATCH

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

        async for event in self._session_store.read_events(
            self._thread_id,
            from_event_id=str(replay_from) if replay_from is not None else None,
        ):
            try:
                await self.handle(event)
            except Exception:  # noqa: BLE001 — replay is best-effort
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
        except Exception:  # noqa: BLE001 — synthesis is best-effort
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
        except Exception:  # noqa: BLE001 — synthesis is best-effort
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

        Sets the verb to ``Thinking…`` so the user sees immediate
        feedback while the engine spins up the first model call.
        """
        _ = event
        await self._app.set_turn_verb("Thinking…", target="")

    async def _handle_thread_exchange_completed(self, event: Event) -> None:
        """Clear the transient turn status; the transcript speaks for itself."""
        _ = event
        await self._app.set_turn_verb("", target="")

    async def _handle_thread_exchange_failed(self, event: Event) -> None:
        """Clear the turn status and surface the failure in the transcript."""
        error = event.data.get("error") or {}
        message = str(error.get("message", "") or error.get("type", "exchange failed"))
        await self._app.set_turn_verb("", target="")
        await self._app.mount_exchange_failed(message)

    async def _handle_user_message(self, event: Event) -> None:
        """Mount a ``UserChatbox`` for the just-submitted turn.

        The adapter mounts the user chatbox from the event so replay
        and live paths share a single mount site.
        """
        content = event.data.get("content") or {}
        text = _extract_text(content)
        if not text:
            return
        await self._app.mount_user_message(text, _now_ts(event))

    async def _handle_agent_message(self, event: Event) -> None:
        """Mount assistant prose only if MODEL_RESPONSE didn't already."""
        content = event.data.get("content") or {}
        text = _extract_text(content)
        if not text:
            return
        # AGENT_MESSAGE fires after MODEL_RESPONSE with the same text
        # for the common case; the adapter defers to MODEL_RESPONSE
        # for streaming or single-shot mount. Only re-mount when the
        # transcript's last assistant text differs — matches the
        # legacy renderer behaviour at `_renderer.py:387`.
        if self._app.last_assistant_text() == text:
            return
        await self._app.mount_assistant_message(text)
        self._app.set_last_assistant_text(text)

    async def _handle_tool_called(self, event: Event) -> None:
        """Capture the call's arguments for the eventual result/failure."""
        tool_call_id = str(event.data.get("tool_call_id", ""))
        if tool_call_id:
            self._tool_call_args[tool_call_id] = dict(
                event.data.get("arguments") or {},
            )
        tool_name = str(event.data.get("tool_name", ""))
        if tool_name:
            args_summary = _format_args(event.data.get("arguments") or {})
            await self._app.set_turn_verb(
                f"Calling {tool_name}",
                target=args_summary,
            )

    async def _handle_tool_result(self, event: Event) -> None:
        """Mount a ``ToolCallBlock`` with the completed result."""
        tool_name = str(event.data.get("tool_name", "<unknown>"))
        tool_call_id = str(event.data.get("tool_call_id", ""))
        args = self._tool_call_args.pop(tool_call_id, {})
        args_str = _format_args(args)
        result = event.data.get("result") or {}
        body = _format_tool_result(result)
        await self._app.mount_tool_call(tool_name, args_str, body, failed=False)
        await self._app.set_turn_verb("Thinking…", target="")

    async def _handle_tool_failed(self, event: Event) -> None:
        """Mount a ``ToolCallBlock`` with ``failed=True`` styling."""
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
        await self._app.mount_tool_call(
            tool_name,
            args_str,
            body,
            failed=True,
            recovery_hint=recovery_hint,
        )

    async def _handle_model_request(self, event: Event) -> None:
        """MODEL_REQUEST fires the "Thinking…" verb.

        No transcript widget mounted — the assistant prose widget
        lands on MODEL_STREAMING_TOKEN (streaming path) or
        MODEL_RESPONSE (non-streaming path).
        """
        _ = event
        await self._app.set_turn_verb("Thinking…", target="")

    async def _handle_model_response(self, event: Event) -> None:
        """Finalise the streaming widget or mount a fresh one."""
        call_id = str(event.data.get("call_id", ""))
        content = event.data.get("content") or {}
        text = _extract_text(content)
        # Streaming path: the widget was mounted on the first
        # STREAMING_TOKEN; nothing further to append.
        if call_id and call_id in self._streaming_widgets:
            self._streaming_widgets.pop(call_id, None)
            self._streaming_buffers.pop(call_id, None)
            if text:
                self._app.set_last_assistant_text(text)
            await self._app.set_turn_verb("Thinking…", target="")
            return
        # Non-streaming path: mount once with the final text.
        if text:
            await self._app.mount_assistant_message(text)
            self._app.set_last_assistant_text(text)
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
        """Surface the fallback transition as a dim italic notice."""
        to = event.data.get("to") or {}
        to_label = (
            f"{to.get('provider', '?')}/{to.get('model', '?')}"
            if isinstance(to, dict)
            else str(to)
        )
        reason = str(event.data.get("reason", "transient model failure"))
        await self._app.mount_notice(f"falling back to {to_label} because {reason}")

    async def _handle_config_set(self, event: Event) -> None:
        """Silent — config is diagnostic-only in the JSONL."""
        _ = event

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
        """Silent — compaction is diagnostic-only."""
        _ = event

    async def _handle_agent_template_warning(self, event: Event) -> None:
        """Surface template warnings as a dim inline notice."""
        message = str(event.data.get("message") or event.data.get("reason", ""))
        if message:
            await self._app.mount_notice(f"template warning: {message}")

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
        try:
            await self._app.mount_approval_block(
                rule_id_val,
                action_val,
                target_val,
            )
        except Exception:  # noqa: BLE001 — mount failure logged, not raised
            _LOGGER.exception(
                "event_adapter: mount_approval_block raised for prompt_id=%r",
                prompt_id,
            )

    def _handle_prompt_answered(self, payload: dict[str, Any]) -> None:
        """Resolve the in-flight `PromptsImpl` future on `.answered`.

        Idempotent per `prompt_id`; a second dispatch is a no-op
        (setting a done future's result would raise
        `InvalidStateError`).
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
        if self._prompts_impl is not None:
            self._prompts_impl.resolve_pending(prompt_id, response)

    def _handle_prompt_cancelled(self, payload: dict[str, Any]) -> None:
        """Cancel the in-flight `PromptsImpl` future on `.cancelled`.

        Raises `asyncio.CancelledError` from the awaiting `choice()`
        / `text()` — Coordinator-locked behaviour matching the Esc
        → deny-once semantics of the shipped approval UX. Idempotent
        per `prompt_id`.
        """
        prompt_id = payload.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            return
        if prompt_id in self._resolved_prompt_ids:
            return
        self._resolved_prompt_ids.add(prompt_id)
        if self._prompts_impl is not None:
            self._prompts_impl.cancel_pending(prompt_id)

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
                return
        # Fallback: no transcript container available (headless
        # service tests) — track in memory via the spec-006 stub.
        entry = _CardMountStub(card_id, kind)
        self._card_widgets[card_id] = entry

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
        except Exception:  # noqa: BLE001 — unknown kind, missing DOM, etc.
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
        except Exception:  # noqa: BLE001 — mount raced with unmount, etc.
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
        """Track notice emission for idempotence; no widget mount here.

        spec-008 lands the widget-mount + `card=` overload. This spec
        just counts the notice so a duplicate dispatch on replay
        doesn't double-mount later.
        """
        _ = event
        raw_id = payload.get("notice_id")
        if not isinstance(raw_id, str) or not raw_id:
            # Legacy notice with no id — dedupe on `event.event_id`.
            raw_id = str(event.event_id)
        if raw_id in self._applied_notice_ids:
            return
        self._applied_notice_ids.add(raw_id)


__all__ = ["CardMountState", "CardTrackingEntry", "EventAdapter"]
