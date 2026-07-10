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
- Appends the event to a bounded ring buffer (`_event_buffer`,
  `maxlen=50`) so a user who expands a row AFTER events have
  arrived can see the recent history (spec-021 — amends spec-013
  Coordinator #3 which dropped collapsed-row events on the floor).
- Mounts the matching sub-agent widget inside the row body when
  the row is expanded. On collapse -> expand transitions, the
  buffer is replayed into the body via `_replay_buffer_to_body`;
  already-mounted event_ids are skipped so live mounts don't
  double up.
- Terminal state transition freezes the buffer (spec-021 AC #7)
  so post-run events are dropped rather than mounted after the
  node has settled.
"""

from __future__ import annotations

import collections
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


_EVENT_BUFFER_MAX = 50
"""spec-021: bounded ring-buffer capacity for sub-agent events.

Sized for ~10 tool calls + ~10 agent messages — enough for a typical
agent turn without unbounded memory. Overflow drops oldest
(``collections.deque(maxlen=...)`` handles it automatically).
"""


_TERMINAL_STATES = frozenset({"completed", "failed", "cancelling", "pruned"})
"""spec-021: states that freeze the event buffer.

On transition into any of these, ``apply_sub_agent_event`` becomes
a no-op — the node has settled and any post-terminal events
would render into an already-finalised row. ``paused`` is NOT
terminal (spec-014 unpauses back to ``running``).
"""


_BASH_SHAPE_KEYS = frozenset({"stdout", "stderr", "exit_code"})
"""bug-034: keys that identify a bash-node output shape.

If a dict's keys are a subset of this set (and it contains at
least one of them), ``_format_content_dict`` renders each field
as its own labeled block with real newlines rather than
JSON-escaped ``\\n`` sequences. Bash nodes ship
``{stdout, stderr, exit_code}``; other shapes fall through to
``json.dumps``.
"""


_BODY_MAX_CHARS = 2000
"""bug-034: total-char cap for rendered node body / content dicts.

Raised from the 500-char bug-032 cap — a diagnostic body needs
enough room to show real stdout without cutting mid-string.
"""


_BASH_BLOCK_MAX_CHARS = 500
"""bug-034: per-block soft cap for stdout / stderr in bash-shape rendering.

Keeps a runaway single stream from dominating the body when both
streams have real content. Applied per-block; the total body
still respects ``_BODY_MAX_CHARS`` (2000). Tuned to 500 so a
single ``"x" * 600`` stream visibly truncates with a trailing
``…`` — matches the bug-032 test's truncation expectation for
the ``_OUTPUT_PREVIEW_MAX_LEN`` surface (see ``set_output``).
"""


def _truncate(text: str, max_chars: int) -> str:
    """Tail-truncate ``text`` at ``max_chars`` with an ellipsis marker.

    A shorter-than-``max_chars`` string round-trips unchanged.
    Reserves one char for the ``…`` suffix so the total rendered
    length stays at the advertised limit.
    """
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _format_content_dict(
    content: dict[str, Any],
    *,
    max_chars: int = _BODY_MAX_CHARS,
) -> str:
    """bug-034: render a content dict as a diagnostic body string.

    Bash-shape dicts — keys ``⊆ {stdout, stderr, exit_code}`` with
    at least one match — render as labeled blocks with **real**
    newlines. This keeps multi-line stdout readable instead of
    surfacing as JSON-escaped ``\\n`` sequences that get chopped
    mid-string by truncation.

    Other shapes fall through to ``json.dumps(indent=2)`` with
    ``default=repr`` so non-serialisable values still render.
    Either branch is tail-truncated to ``max_chars``.
    """
    if _is_bash_shape(content):
        return _truncate(_render_bash_blocks(content), max_chars)
    try:
        pretty = json.dumps(content, indent=2, default=repr)
    except (TypeError, ValueError):
        pretty = repr(content)
    return _truncate(pretty, max_chars)


def _is_bash_shape(content: Any) -> bool:
    """Return True if ``content`` looks like a bash node output dict.

    Requires: a dict whose keys are a subset of
    ``{stdout, stderr, exit_code}`` and that contains at least one
    of those keys. An empty dict or a dict with extra keys returns
    False so we don't accidentally hijack an unrelated shape.
    """
    if not isinstance(content, dict) or not content:
        return False
    keys = set(content.keys())
    if not keys.issubset(_BASH_SHAPE_KEYS):
        return False
    return bool(keys & _BASH_SHAPE_KEYS)


def _render_bash_blocks(content: dict[str, Any]) -> str:
    """Render a bash-shape dict as labeled blocks with real newlines.

    Ordering: ``exit_code`` first (one-liner header), then
    ``stdout``, then ``stderr``. Empty streams are omitted so a
    silent-stderr run doesn't waste rows on an empty "stderr:"
    label. Each stream body is tail-truncated at
    ``_BASH_BLOCK_MAX_CHARS`` to keep one runaway stream from
    swallowing the body.
    """
    lines: list[str] = []
    if "exit_code" in content:
        lines.append(f"exit_code: {content['exit_code']}")
    stdout = content.get("stdout")
    if isinstance(stdout, str) and stdout:
        lines.append("")
        lines.append("stdout:")
        lines.append(_truncate(stdout, _BASH_BLOCK_MAX_CHARS))
    stderr = content.get("stderr")
    if isinstance(stderr, str) and stderr:
        lines.append("")
        lines.append("stderr:")
        lines.append(_truncate(stderr, _BASH_BLOCK_MAX_CHARS))
    return "\n".join(lines)


def _extract_content_text(content: Any) -> str:
    """bug-034: pull a display string out of a sub-agent ``content`` value.

    Fallback chain per spec:
      1. ``content.text`` — the conventional session-event shape.
      2. ``content.output`` / ``content.result`` — agent structured
         outputs (final answers from tool-heavy nodes).
      3. Readable rendering of the whole dict via
         ``_format_content_dict`` — covers workflow IOSpec inputs
         (``{"input": "..."}``) and other free-shape dicts where
         no single key holds the display string.
      4. Empty string when ``content`` is empty or missing entirely.

    Non-dict content (a bare string, a list) falls back to
    ``str(content)`` truncated to the body cap so callers still get
    something readable rather than an empty widget.
    """
    if content is None or content == "":
        return ""
    if isinstance(content, dict):
        if not content:
            return ""
        for key in ("text", "output", "result"):
            value = content.get(key)
            if isinstance(value, str) and value:
                return value
        return _format_content_dict(content)
    return _truncate(str(content), _BODY_MAX_CHARS)


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

    _OUTPUT_PREVIEW_MAX_LEN = _BODY_MAX_CHARS
    """bug-034: pretty-printed `payload.output` truncation cap.

    Raised from bug-032's 500 to 2000 so multi-line bash stdout
    renders in full instead of getting chopped mid-string. The
    per-block cap (``_BASH_BLOCK_MAX_CHARS = 500``) keeps a runaway
    stream from swallowing the body. Longer content gets a trailing
    ``…`` marker so the user knows there is more.
    """

    state = reactive("starting", init=False)
    """Reactive; `watch_state` applies the `.-<state>` CSS class."""

    def __init__(
        self,
        *,
        node_name: str,
        trace_id: str | None = None,
        node_type: str | None = None,
    ) -> None:
        self._node_name = node_name
        self._trace_id = trace_id
        # bug-036a: node type string from ``NODE_STARTED.payload.type``
        # (e.g. ``"kolsch/nodes/agent"``). Used by ``set_output`` to
        # skip the JSON preview when the row already rendered the
        # agent's response via ``agent.message`` → ``AssistantProse``.
        # bug-036b will reuse this same slot for the title suffix.
        self._node_type = node_type
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
        # spec-021: bounded ring buffer for sub-agent events. Grows
        # while the row is collapsed (or after live mounts, so future
        # collapse -> expand cycles can replay). Overflow drops the
        # oldest entry automatically (``deque(maxlen=...)``).
        self._event_buffer: collections.deque[Any] = collections.deque(
            maxlen=_EVENT_BUFFER_MAX,
        )
        # spec-021: dedupe key set for events already mounted into the
        # body. Consulted by ``_replay_buffer_to_body`` so a re-expand
        # after a live-expanded run doesn't double-mount the widgets
        # that arrived while the row was already open.
        self._mounted_event_ids: set[Any] = set()
        # spec-021: latch flipped by ``watch_state`` on terminal
        # transition. Once frozen, ``apply_sub_agent_event`` short-
        # circuits — no append, no mount, no turn-count bump.
        self._buffer_frozen: bool = False
        # bug-036a: latched by ``apply_sub_agent_event`` when the
        # inner event is ``agent.message``. Read by ``set_output`` to
        # skip the JSON-preview fallback for agent nodes whose
        # response already rendered via ``AssistantProse``.
        self._saw_agent_message: bool = False
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
        """bug-032 + bug-034: mount a pretty-printed preview of `output`.

        Fired by
        [`WorkflowRenderer._on_node_completed`][cli.chat.widgets.card_body.workflow.WorkflowRenderer._on_node_completed]
        when a `WorkflowNodeCompletedEvent`'s `payload.output` is
        present. Idempotent — a second call removes the previously
        mounted widget before mounting the new one so the row body
        never accumulates stale previews.

        bug-034: bash-shape outputs (keys ⊆
        ``{stdout, stderr, exit_code}``) render as labeled blocks
        with **real** newlines via ``_format_content_dict`` — no
        more JSON-escaped ``\\n`` mid-line cuts. Other shapes fall
        through to the same helper's ``json.dumps`` branch.

        No-op when `output` is `None`, an empty dict, or an empty
        string — nothing worth showing.
        """
        # No-op guard: `None`, `{}`, `""`. Non-empty falsy shapes
        # like `0` or `False` would also fail an ordinary truthy
        # check, so match on the exact three "nothing worth showing"
        # values enumerated in the spec.
        if output is None:
            return
        if isinstance(output, (dict, list, str)) and not output:
            return
        # bug-036a: agent nodes render their response via
        # ``agent.message`` → ``AssistantProse`` in the sub-agent
        # stream. The completed-node ``payload.output`` echoes the
        # same content as JSON — showing both is the duplicate the
        # user reported. Skip the fallback when the row already
        # observed at least one ``agent.message``.
        if self._node_type == "kolsch/nodes/agent" and self._saw_agent_message:
            return
        if isinstance(output, dict):
            pretty = _format_content_dict(
                output, max_chars=self._OUTPUT_PREVIEW_MAX_LEN,
            )
        else:
            try:
                pretty = json.dumps(output, indent=2, default=repr)
            except (TypeError, ValueError):
                pretty = repr(output)
            pretty = _truncate(pretty, self._OUTPUT_PREVIEW_MAX_LEN)
        # Idempotency: drop the previous preview mount before adding
        # the new one. A failed remove (row not yet mounted, previous
        # widget already detached) must not block the new mount.
        if self._output_widget is not None:
            with contextlib.suppress(Exception):
                self._output_widget.remove()
            self._output_widget = None
        widget = Static(pretty)
        self._output_widget = widget
        # bug-035: mount inside the Collapsible's ``Contents`` slot
        # so the outer collapse/expand actually hides the preview.
        # ``self.mount(widget)`` on a Collapsible mounts as a sibling
        # of ``Contents``; only ``Contents`` responds to the
        # ``.-collapsed`` CSS rule.
        with contextlib.suppress(Exception):
            self._mount_in_contents(widget)

    def apply_sub_agent_event(self, event: Any) -> None:
        """Route a sub-agent `Event` to this row.

        spec-021: unconditionally append to the ring buffer + bump
        the turn count; when EXPANDED, also mount the widget live.
        When COLLAPSED, the event lives in ``_event_buffer`` until
        ``watch_collapsed`` fires ``_replay_buffer_to_body``.

        Terminal-state rows short-circuit: once ``_buffer_frozen``
        is set (by ``watch_state`` on the terminal transition), any
        further sub-agent events are dropped — no append, no mount,
        no count bump. This keeps a settled row from mutating.
        """
        if self._buffer_frozen:
            return
        self._event_buffer.append(event)
        # bug-036a: latch the "saw an agent.message" flag BEFORE the
        # collapsed check so a row that stays collapsed through the
        # whole run still gates ``set_output`` correctly.
        if self._extract_inner_event_type(event) == "agent.message":
            self._saw_agent_message = True
        self._sub_agent_turn_count += 1
        self._update_title()
        if not self.collapsed:
            self._mount_sub_agent_widget(event)

    @staticmethod
    def _extract_inner_event_type(event: Any) -> str:
        """Return the inner event's type as a string, or ``""``.

        bug-036a: both the real pydantic ``Event`` and
        ``_ReconstructedSubAgentEvent`` (workflow.py) expose ``type``.
        The real ``Event.type`` is an ``EventType`` enum whose
        ``value`` is the wire string (e.g. ``"agent.message"``); the
        reconstructed stub carries the enum itself. Coerce both to
        the string surface.
        """
        raw = getattr(event, "type", None)
        if raw is None:
            return ""
        value = getattr(raw, "value", None)
        if isinstance(value, str):
            return value
        return str(raw)

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

        spec-021: on transition into any terminal state (``completed``
        / ``failed`` / ``cancelling`` / ``pruned``) freeze the event
        buffer so any post-terminal ``apply_sub_agent_event`` calls
        become no-ops. ``paused`` is deliberately excluded — the
        row can resume back to ``running`` via spec-014.
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
        # spec-021: freeze the event buffer on terminal transition.
        if new in _TERMINAL_STATES:
            self._buffer_frozen = True

    def watch_collapsed(self, old: bool, new: bool) -> None:
        """spec-021: replay the event buffer into the body on expand.

        Fired by Textual's reactive machinery whenever the parent
        ``Collapsible.collapsed`` reactive flips. The parent's own
        ``_watch_collapsed`` still runs alongside this (private +
        public watchers are dispatched independently), so the
        ``Expanded`` / ``Collapsed`` messages continue to post and
        the ``-collapsed`` CSS class continues to toggle — this
        override only adds the buffer-replay side effect on the
        collapsed -> expanded edge.
        """
        _ = old
        if not new:
            self._replay_buffer_to_body()

    def _replay_buffer_to_body(self) -> None:
        """Mount any un-mounted buffered events into the body.

        spec-021: iterates ``_event_buffer`` in insertion order and
        forwards each event to ``_mount_sub_agent_widget`` unless
        its dedupe key already sits in ``_mounted_event_ids``. The
        guard set is what lets live-mount + re-expand cycles work
        without duplicating widgets — events that arrived while the
        row was expanded live-mount immediately (which also stamps
        the dedupe key), and a later collapse -> expand cycle finds
        them already stamped and skips the replay.
        """
        for event in list(self._event_buffer):
            key = self._event_dedupe_key(event)
            if key in self._mounted_event_ids:
                continue
            self._mount_sub_agent_widget(event)

    @staticmethod
    def _event_dedupe_key(event: Any) -> Any:
        """Return a stable per-event key for the mount dedupe set.

        Real pydantic ``Event`` objects carry ``event_id`` (a string
        newtype). The wire-reconstructed stub in
        ``cli.chat.widgets.card_body.workflow._ReconstructedSubAgentEvent``
        deliberately omits it (see the stub's docstring), so we fall
        back to ``id(event)`` — the CPython object identity is stable
        for the life of the event object, which is long enough because
        the same event instance is what's stored in the buffer.
        """
        event_id = getattr(event, "event_id", None)
        if event_id is not None:
            return str(event_id)
        return id(event)

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

        bug-034: content extraction uses ``_extract_content_text``
        (fallback chain: ``content.text`` -> ``content.output`` /
        ``content.result`` -> readable dict rendering) so
        workflow-invoked agents whose ``data.content`` is IOSpec
        inputs (``{"input": "..."}``) or structured outputs
        (``{"top_10_requirements": [...]}``) render a real preview
        rather than an empty box. TOOL_CALLED / TOOL_RESULT now
        pull ``data.arguments`` into the title and ``data.result``
        into the body via the shared bash-aware helper.
        """
        # Deferred import — avoid a widget-layer cycle on module
        # load. spec-013 mounts stub-compatible widgets; full
        # per-type wiring lands with the sub-agent renderer work.
        try:
            from schemas.events import EventType  # noqa: PLC0415
        except Exception:  # noqa: BLE001 — import guard for headless tests
            return
        event_type = getattr(event, "type", None)
        data: dict[str, Any] = {}
        raw_data = getattr(event, "data", None)
        if isinstance(raw_data, dict):
            data = raw_data
        text = self._extract_message_text(data)

        widget: Any | None = None
        try:
            if event_type == EventType.USER_MESSAGE:
                # bug-035: inside a workflow node, USER_MESSAGE is the
                # scheduler's invocation payload — not a real user
                # turn. Rendering a full ``UserChatbox`` ("You: ...")
                # was semantically wrong and cluttered the body. Skip
                # it: the row's collapsed title already shows the node
                # name; the agent's response + tool calls carry the
                # useful narrative. Turn counter still increments in
                # ``apply_sub_agent_event`` so the title reflects
                # activity.
                return
            elif event_type == EventType.AGENT_MESSAGE:
                from cli.chat.widgets.assistant_prose import AssistantProse  # noqa: PLC0415

                widget = AssistantProse(text)
            elif event_type in (EventType.TOOL_CALLED, EventType.TOOL_RESULT):
                from cli.chat.widgets.tool_call_block import ToolCallBlock  # noqa: PLC0415

                tool_name = ""
                with contextlib.suppress(Exception):
                    tool_name = str(data.get("tool_name") or "")
                args_str, body = self._extract_tool_call_parts(
                    event_type, data, text,
                )
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
        # bug-035: mount inside the Collapsible's ``Contents`` slot so
        # the outer collapse/expand actually controls visibility. See
        # ``_mount_in_contents`` for the fallback chain used when the
        # slot isn't yet resolvable (headless tests, pre-mount setup).
        with contextlib.suppress(Exception):
            self._mount_in_contents(widget)
        # spec-021: stamp the dedupe key AFTER attempting the mount so
        # a live-expanded arrival + later re-expand skips a duplicate
        # replay. We stamp even on a swallowed mount failure — the
        # buffer replay is best-effort and re-mounting a widget that
        # already failed once won't get further, so treat "attempted"
        # as "mounted" for dedupe purposes.
        self._mounted_event_ids.add(self._event_dedupe_key(event))

    def _mount_in_contents(self, widget: Widget) -> None:
        """bug-035: mount ``widget`` inside the Collapsible's Contents slot.

        Textual's :class:`Collapsible.compose_add_child` only wires
        the *context-manager* compose syntax to the internal
        ``Contents`` container — a bare ``self.mount(child)`` on an
        already-composed Collapsible mounts as a **sibling** of the
        ``CollapsibleTitle`` and ``Contents``. Only ``Contents``
        responds to the ``.-collapsed > Contents { display: none }``
        CSS, so sibling mounts stay visible regardless of the row's
        collapsed state — which is exactly the "collapse doesn't
        control the content" bug the user reported.

        Fallback chain: prefer the composed ``Contents`` when it can
        be resolved; on failure (e.g. the row hasn't finished
        mounting) fall through to ``self.mount(widget)`` so headless
        tests + pre-mount setup still land somewhere. The buffer
        replay on expand will re-attempt the correct slot when the
        row is fully composed.
        """
        try:
            contents = self.query_one(Collapsible.Contents)
        except Exception:  # noqa: BLE001 — Contents not composed yet
            contents = None
        if contents is not None:
            contents.mount(widget)
            return
        # Row not fully mounted yet — mount at the outer level. This
        # is only reachable in headless test paths that never enter
        # the Textual mount lifecycle.
        self.mount(widget)

    @staticmethod
    def _extract_message_text(data: dict[str, Any]) -> str:
        """bug-034: fallback chain for USER_MESSAGE / AGENT_MESSAGE bodies.

        Priority order (per spec AC #3):
          1. ``content.text`` — canonical session-event shape.
          2. ``content.output`` / ``content.result`` — agent
             structured outputs.
          3. Readable dict rendering of ``content`` (bash-aware).
          4. ``text_delta`` fallback for streaming shapes that
             land here despite the drop-set filter.
          5. Empty string when nothing usable is on the event.
        """
        content = data.get("content")
        text = _extract_content_text(content)
        if text:
            return text
        delta = data.get("text_delta")
        if isinstance(delta, str) and delta:
            return delta
        return ""

    @staticmethod
    def _extract_tool_call_parts(
        event_type: Any,
        data: dict[str, Any],
        text: str,
    ) -> tuple[str, str]:
        """bug-034: pull ``args_str`` + ``body`` for a ToolCallBlock.

        TOOL_CALLED: ``data.arguments`` populates ``args_str``
        (compact JSON, truncated for the title) so ``● bash(...)``
        actually shows the invocation. TOOL_RESULT: ``data.result``
        populates the body via the shared bash-aware helper so
        multi-line stdout renders with real newlines.

        The body falls back to the extracted message ``text`` when
        no ``result`` is on the event, keeping any pre-existing
        content-based rendering usable.
        """
        # Deferred import — schema alias only needed for the branch
        # discrimination; avoids paying for it on every call.
        from schemas.events import EventType  # noqa: PLC0415

        args_str = ""
        body = text

        arguments = data.get("arguments")
        if arguments is None:
            arguments = data.get("args")
        if isinstance(arguments, dict) and arguments:
            with contextlib.suppress(TypeError, ValueError):
                # Compact JSON keeps the title one-liner; the full
                # dict lands in the body only for TOOL_CALLED where
                # there's no result yet to occupy the space.
                args_str = _truncate(
                    json.dumps(arguments, default=repr, sort_keys=True),
                    60,
                )
        elif isinstance(arguments, str) and arguments:
            args_str = _truncate(arguments, 60)

        if event_type == EventType.TOOL_RESULT:
            result = data.get("result")
            if isinstance(result, dict) and result:
                body = _format_content_dict(result)
            elif isinstance(result, str) and result:
                body = _truncate(result, _BODY_MAX_CHARS)
            elif not body:
                # No result + no fallback text — surface ``output``
                # if present (some tools ship the result under that
                # key). Otherwise leave the body empty.
                output = data.get("output")
                if isinstance(output, str) and output:
                    body = _truncate(output, _BODY_MAX_CHARS)
                elif isinstance(output, dict) and output:
                    body = _format_content_dict(output)
        elif event_type == EventType.TOOL_CALLED and not body:
            # TOOL_CALLED: pre-run, no result to show. Put the full
            # arguments dict in the body so the expanded row has
            # something to render beyond the title.
            if isinstance(arguments, dict) and arguments:
                body = _format_content_dict(arguments)

        return args_str, body


__all__ = ["WorkflowNodeRow"]
