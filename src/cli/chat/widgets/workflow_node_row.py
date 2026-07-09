"""`WorkflowNodeRow` — per-node collapsible row inside a workflow card.

spec-013 §WorkflowNodeRow. Mounted inside a
[`WorkflowNodeList`][cli.chat.widgets.card_body.workflow.WorkflowNodeList]
by [`WorkflowRenderer.apply_event`][cli.chat.widgets.card_body.workflow.WorkflowRenderer.apply_event]
on `workflow.node.started`. The row is a
[`Collapsible`][textual.widgets.Collapsible] with a reactive
`state` field driving CSS class + glyph.

State machine (7 states, spec-013 §State table):

- `starting` (`⠋`) — freshly mounted, no CSS class.
- `running` (`▸`) — sub-agent activity observed.
- `completed` (`✓`) — terminal success, class `.-completed`.
- `failed` (`✗`) — terminal failure, class `.-failed`; the
  collapsed title carries an inline error preview truncated to
  60 chars (Coordinator #4).
- `paused` (`⚠`) — awaiting approval (spec-014 wires the trigger),
  class `.-paused`.
- `cancelling` (`⧗`) — run cancel fired, node unwinding, class
  `.-cancelling`.
- `pruned` (`─`) — node never ran (branch cascade), class
  `.-pruned`.

Sub-agent event routing lands via
[`apply_sub_agent_event`][cli.chat.widgets.workflow_node_row.WorkflowNodeRow.apply_sub_agent_event]:

- Increments the row's `_sub_agent_turn_count` on every call.
- Mounts the matching sub-agent widget inside the collapsed body
  ONLY when the row is expanded (Coordinator #3 lock — no
  buffering; events received while collapsed are counted, not
  buffered).
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from typing import Any

from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Collapsible, Static


_SPINNER_FRAMES = ("⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷")
"""bug-031: 8-cell braille spinner frames rotated at ~12.5 Hz.

Matches the convention established by
``cli.chat.widgets.turn_status._SPINNER_FRAMES`` (module-level
constant, ``set_interval(0.08, ...)``). Frame rotation drives the
"card is animating" cue the user reads to know a running node
hasn't stalled — session ``ses_af5980ab`` had a 90.4s dead zone on
the ``deduper`` node with the static ``▸`` glyph, indistinguishable
from a hang.
"""


_ANIMATED_STATES = frozenset({"starting", "running"})
"""bug-031: states whose title uses the spinner frame instead of the
static glyph. Terminal states (``completed`` / ``failed`` /
``paused`` / ``cancelling`` / ``pruned``) use the static glyph as
today — the row is done moving.
"""


class WorkflowNodeRow(Collapsible):
    """Per-node row inside a `WorkflowNodeList`.

    Constructor is keyword-only: `node_name` (title anchor) +
    optional `trace_id` (routing key for sub-agent events landing
    on the `EventAdapter`). Starts collapsed with state=`starting`.
    """

    DEFAULT_CSS = """
    WorkflowNodeRow { padding: 0 1; margin: 0; }
    WorkflowNodeRow.-completed  { color: #4dd0e1; }
    WorkflowNodeRow.-failed     { color: #ff5555; }
    WorkflowNodeRow.-paused     { color: #ffb454; }
    WorkflowNodeRow.-cancelling { color: #4dd0e1; opacity: 0.6; }
    WorkflowNodeRow.-pruned     { color: #6e7681; }
    """

    STATE_GLYPHS = {
        "starting":   "⠋",
        "running":    "▸",
        "completed":  "✓",
        "failed":     "✗",
        "paused":     "⚠",
        "cancelling": "⧗",
        "pruned":     "─",
    }
    """7-state glyph map. Keys mirror the spec's state table."""

    _TERMINAL_CSS_STATES = ("completed", "failed", "paused", "cancelling", "pruned")
    """States that map to a `.-<state>` CSS class. `starting` +
    `running` render with the default palette.
    """

    _ERROR_PREVIEW_MAX_LEN = 60
    """Coordinator #4 lock — fixed 60 char error preview truncation."""

    _OUTPUT_PREVIEW_MAX_LEN = 500
    """bug-032: pretty-printed `payload.output` is truncated to this
    many chars in the expanded body (spec says ~500). Longer content
    gets a trailing ``…`` marker so the user knows there is more.
    """

    state = reactive("starting", init=False)
    """Reactive; `watch_state` applies the `.-<state>` CSS class."""

    def __init__(
        self,
        *,
        node_name: str,
        trace_id: str | None = None,
    ) -> None:
        self._node_name = node_name
        self._trace_id = trace_id
        self._sub_agent_turn_count = 0
        self._error_preview: str | None = None
        self._details: str | None = None
        # bug-031: rotating spinner frame index. ``_advance_spinner``
        # bumps this on the 0.08s interval; ``_update_title`` reads it
        # into the collapsed-title glyph while ``state`` is animated.
        self._frame_idx: int = 0
        # bug-031: spinner interval handle. Installed by ``on_mount``;
        # stopped by ``watch_state`` when the row transitions to a
        # terminal state so the widget goes quiescent post-run.
        self._spinner_handle: Any | None = None
        # bug-032: reference to the last-mounted output preview
        # widget so ``set_output`` can remove the previous mount
        # before mounting a fresh one (idempotent).
        self._output_widget: Widget | None = None
        title = f"{self.STATE_GLYPHS['starting']} {node_name}"
        super().__init__(title=title, collapsed=True)

    # ------------------------------------------------------------------
    # Public state surface
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        """bug-031: install the ~12.5 Hz spinner-frame timer.

        Mirrors the convention at
        ``cli.chat.widgets.turn_status.TurnStatus._start_timers``
        (line 201, ``set_interval(0.08, self._advance_spinner)``).
        The interval runs unconditionally — ``_update_title`` only
        surfaces the spinner while ``self.state`` is in
        ``_ANIMATED_STATES``. On the terminal state transition,
        ``watch_state`` stops the handle.
        """
        self._spinner_handle = self.set_interval(0.08, self._advance_spinner)

    def _advance_spinner(self) -> None:
        """bug-031: bump the frame index and repaint the title.

        Terminal-state rows short-circuit inside ``_update_title``
        (they use the static glyph). Keeping the tick running past
        the transition until ``watch_state`` stops it is harmless.
        """
        self._frame_idx = (self._frame_idx + 1) % len(_SPINNER_FRAMES)
        if self.state in _ANIMATED_STATES:
            self._update_title()

    def set_state(
        self,
        state: str,
        *,
        details: str | None = None,
        error_preview: str | None = None,
    ) -> None:
        """Transition the row to `state`; refresh title + CSS.

        `details` populates the completed-row inline summary (e.g.
        `"42 files scanned"`). `error_preview` populates the failed-
        row inline preview and is truncated to 60 chars in the
        collapsed title.
        """
        if details is not None:
            self._details = details
        if error_preview is not None:
            self._error_preview = error_preview
        self.state = state
        self._update_title()

    def set_output(self, output: Any) -> None:
        """bug-032: mount a pretty-printed preview of `output` in the body.

        Fired by
        [`WorkflowRenderer._on_node_completed`][cli.chat.widgets.card_body.workflow.WorkflowRenderer._on_node_completed]
        when a `WorkflowNodeCompletedEvent`'s `payload.output` is
        present. Idempotent — a second call removes the previously
        mounted widget before mounting the new one so the row body
        never accumulates stale previews.

        No-op when `output` is `None`, an empty dict, or an empty
        string — nothing worth showing. Non-JSON-serialisable values
        fall back to `repr()` so the widget still renders instead of
        raising during mount.
        """
        # No-op guard: `None`, `{}`, `""`. Non-empty falsy shapes
        # like `0` or `False` would also fail an ordinary truthy
        # check, so match on the exact three "nothing worth showing"
        # values enumerated in the spec.
        if output is None:
            return
        if isinstance(output, (dict, list, str)) and not output:
            return
        try:
            pretty = json.dumps(output, indent=2, default=repr)
        except (TypeError, ValueError):
            pretty = repr(output)
        if len(pretty) > self._OUTPUT_PREVIEW_MAX_LEN:
            # Reserve one char for the trailing ellipsis so the total
            # rendered length stays at the advertised limit.
            pretty = pretty[: self._OUTPUT_PREVIEW_MAX_LEN - 1] + "…"
        # Idempotency: drop the previous preview mount before adding
        # the new one. A failed remove (row not yet mounted, previous
        # widget already detached) must not block the new mount.
        if self._output_widget is not None:
            with contextlib.suppress(Exception):
                self._output_widget.remove()
            self._output_widget = None
        widget = Static(pretty)
        self._output_widget = widget
        with contextlib.suppress(Exception):
            self.mount(widget)

    def apply_sub_agent_event(self, event: Any) -> None:
        """Route a sub-agent `Event` to this row.

        Coordinator #3 lock: events received while COLLAPSED only
        increment `_sub_agent_turn_count` (no buffering, no
        deferred mount). When EXPANDED, `_mount_sub_agent_widget`
        renders the matching widget inside the row's body.
        """
        self._sub_agent_turn_count += 1
        self._update_title()
        if not self.collapsed:
            self._mount_sub_agent_widget(event)

    # ------------------------------------------------------------------
    # Reactive + rendering
    # ------------------------------------------------------------------

    def watch_state(self, old: str, new: str) -> None:
        """Reactive watcher — apply `.-<state>` CSS class.

        The default `starting` / `running` states render without a
        `.-<state>` suffix — bare CSS covers them.

        bug-031: on transition to a terminal state stop the spinner
        timer so the widget goes quiescent (no wake-ups after the
        node has settled). The ``starting`` -> ``running`` transition
        keeps the timer alive.
        """
        _ = old
        for cls in self._TERMINAL_CSS_STATES:
            self.remove_class(f"-{cls}")
        if new in self._TERMINAL_CSS_STATES:
            self.add_class(f"-{new}")
        # bug-031: terminal transition — stop the spinner frames.
        if new not in _ANIMATED_STATES and self._spinner_handle is not None:
            with contextlib.suppress(Exception):
                self._spinner_handle.stop()
            self._spinner_handle = None

    def _update_title(self) -> None:
        """Refresh the collapsed title from state + details + count.

        Format: `{glyph} {node_name}   {details_or_error}` +
        ` · N sub-agent turns` when count > 0. Failed rows prefer the
        truncated `_error_preview`; completed / running rows use
        `_details`.

        bug-031: while ``self.state`` is animated (``starting`` /
        ``running``) the glyph is the current braille spinner frame
        so the row visibly ticks even during long-running nodes.
        Terminal states retain their static ``STATE_GLYPHS`` entry.
        """
        if self.state in _ANIMATED_STATES:
            glyph = _SPINNER_FRAMES[self._frame_idx]
        else:
            glyph = self.STATE_GLYPHS.get(self.state, "?")
        base = f"{glyph} {self._node_name}"
        if self.state == "failed" and self._error_preview:
            base += f"   {self._truncate_preview(self._error_preview)}"
        elif self._details:
            base += f"   {self._details}"
        if self._sub_agent_turn_count > 0:
            base += f" · {self._sub_agent_turn_count} sub-agent turns"
        self.title = base

    def _truncate_preview(
        self,
        text: str,
        max_len: int = _ERROR_PREVIEW_MAX_LEN,
    ) -> str:
        """Tail-truncate `text` at `max_len` chars (default 60).

        Preserves the leading `max_len - 3` chars and appends `...`.
        A shorter-than-`max_len` string round-trips unchanged.
        """
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + "..."

    # ------------------------------------------------------------------
    # Sub-agent mount hook
    # ------------------------------------------------------------------

    def _mount_sub_agent_widget(self, event: Any) -> None:
        """Mount the sub-agent widget matching `event.type` in the row.

        Best-effort — headless tests exercise `apply_sub_agent_event`
        without a live mount tree, so a mount failure is swallowed.
        The concrete widget (`UserChatbox`, `AssistantProse`,
        `ToolCallBlock`) is selected from `event.type`; unrecognised
        types are silently dropped.
        """
        # Deferred import — avoid a widget-layer cycle on module
        # load. spec-013 mounts stub-compatible widgets; full
        # per-type wiring lands with the sub-agent renderer work.
        try:
            from schemas.events import EventType  # noqa: PLC0415
        except Exception:  # noqa: BLE001 — import guard for headless tests
            return
        event_type = getattr(event, "type", None)
        text = ""
        with contextlib.suppress(Exception):
            data = getattr(event, "data", None) or {}
            if isinstance(data, dict):
                content = data.get("content") or {}
                if isinstance(content, dict):
                    text = str(content.get("text") or "")
                if not text:
                    text = str(data.get("text_delta") or "")

        widget: Any | None = None
        try:
            if event_type == EventType.USER_MESSAGE:
                from cli.chat.widgets.user_chatbox import UserChatbox  # noqa: PLC0415

                ts_val = getattr(event, "created_at", None)
                if not isinstance(ts_val, datetime):
                    ts_val = datetime.now(tz=UTC)
                widget = UserChatbox(text or "", ts_val)
            elif event_type == EventType.AGENT_MESSAGE:
                from cli.chat.widgets.assistant_prose import AssistantProse  # noqa: PLC0415

                widget = AssistantProse(text or "")
            elif event_type in (EventType.TOOL_CALLED, EventType.TOOL_RESULT):
                from cli.chat.widgets.tool_call_block import ToolCallBlock  # noqa: PLC0415

                tool_name = ""
                args_str = ""
                body = text or ""
                with contextlib.suppress(Exception):
                    data = getattr(event, "data", None) or {}
                    if isinstance(data, dict):
                        tool_name = str(data.get("tool_name") or "")
                widget = ToolCallBlock(
                    tool_name or "?",
                    args_str,
                    body,
                    failed=False,
                )
        except Exception:  # noqa: BLE001 — widget construction is optional
            return
        if widget is None:
            return
        with contextlib.suppress(Exception):
            self.mount(widget)


__all__ = ["WorkflowNodeRow"]
