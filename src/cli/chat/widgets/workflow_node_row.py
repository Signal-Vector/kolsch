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

bug-036d — sink-consolidation refactor
--------------------------------------

Prior to bug-036d, sub-agent events reached this row via a bespoke
``apply_sub_agent_event`` -> ``_mount_sub_agent_widget`` chain that
duplicated the main chat's dispatch. bug-036d collapses the two
paths into one: the row implements
:class:`~cli.chat.services.mount_sink.ChatMountSink`, and the
adapter's normal handlers pick either the App or a row as sink.

- ``_saw_agent_message`` (bug-036a flag) → ``_last_assistant_text``
  (string). Empty text means "nothing rendered"; non-empty means
  the ``set_output`` gate should fire (agent nodes).
- ``_event_buffer`` (raw events) → ``_pending_mounts`` (mount
  closures). Same bounded shape, replayed sequentially on expand.
- ``apply_sub_agent_event`` / ``_mount_sub_agent_widget`` — GONE.
  The renderer and adapter now unwrap the outer envelope and call
  the sink's typed methods directly.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable

from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Collapsible, Static

_LOGGER = logging.getLogger(__name__)


_SPINNER_FRAMES = ("⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷")
"""bug-031: 8-cell braille spinner frames rotated at ~12.5 Hz."""


_ANIMATED_STATES = frozenset({"starting", "running"})
"""bug-031: states whose title uses the spinner frame."""


_EVENT_BUFFER_MAX = 50
"""spec-021: bounded queue capacity for pending mount closures.

bug-036d renames the concept from "event buffer" to "pending
mounts" but keeps the 50-slot bound so a collapsed row can't
grow unbounded while a sub-agent runs.
"""


_TERMINAL_STATES = frozenset({"completed", "failed", "cancelling", "pruned"})
"""spec-021: states that freeze the pending-mount queue."""


_BASH_SHAPE_KEYS = frozenset({"stdout", "stderr", "exit_code"})
"""bug-034: keys that identify a bash-node output shape."""


_BODY_MAX_CHARS = 2000
"""bug-034: total-char cap for rendered node body / content dicts."""


_BASH_BLOCK_MAX_CHARS = 500
"""bug-034: per-block soft cap for stdout / stderr."""


def _truncate(text: str, max_chars: int) -> str:
    """Tail-truncate ``text`` at ``max_chars`` with an ellipsis marker."""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _format_inputs_summary(
    inputs: dict[str, Any],
    *,
    max_chars: int = 80,
) -> str:
    """bug-036c: compact single-line summary for the WorkflowCard header."""
    if not inputs:
        return ""
    first_key = next(iter(inputs))
    first_value = inputs[first_key]
    value_preview = _format_input_value(first_value)
    if len(inputs) == 1:
        summary = f"{first_key}={value_preview}"
    else:
        summary = f"{len(inputs)} inputs: {first_key}={value_preview}"
    return _truncate(summary, max_chars)


def _format_input_value(value: Any) -> str:
    """bug-036c: preview a single input value for the header summary."""
    if isinstance(value, str):
        collapsed = value.replace("\n", " ⏎ ")
        total = len(value)
        if total > 60:
            preview = _truncate(collapsed, 30)
            return f"{preview} ({total} chars)"
        return _truncate(collapsed, 30)
    if isinstance(value, (dict, list)):
        try:
            rendered = json.dumps(value, default=repr, ensure_ascii=False)
        except (TypeError, ValueError):
            rendered = repr(value)
        return _truncate(rendered, 30)
    return _truncate(str(value), 30)


def _format_content_dict(
    content: dict[str, Any],
    *,
    max_chars: int = _BODY_MAX_CHARS,
) -> str:
    """bug-034: render a content dict as a diagnostic body string."""
    if _is_bash_shape(content):
        return _truncate(_render_bash_blocks(content), max_chars)
    try:
        pretty = json.dumps(content, indent=2, default=repr)
    except (TypeError, ValueError):
        pretty = repr(content)
    return _truncate(pretty, max_chars)


def _is_bash_shape(content: Any) -> bool:
    """Return True if ``content`` looks like a bash node output dict."""
    if not isinstance(content, dict) or not content:
        return False
    keys = set(content.keys())
    if not keys.issubset(_BASH_SHAPE_KEYS):
        return False
    return bool(keys & _BASH_SHAPE_KEYS)


def _render_bash_blocks(content: dict[str, Any]) -> str:
    """Render a bash-shape dict as labeled blocks with real newlines."""
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
    """bug-034: pull a display string out of a sub-agent ``content`` value."""
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

    bug-036d: implements
    :class:`~cli.chat.services.mount_sink.ChatMountSink`. The
    adapter's normal handlers pick this row as the sink target when
    a session event's ``(trace_id, invocation_id)`` matches — same
    widget constructions as the main App, buffered while collapsed.
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
    """7-state glyph map."""

    _TERMINAL_CSS_STATES = ("completed", "failed", "paused", "cancelling", "pruned")
    """States that map to a `.-<state>` CSS class."""

    _ERROR_PREVIEW_MAX_LEN = 60
    """Coordinator #4 lock — fixed 60 char error preview truncation."""

    _OUTPUT_PREVIEW_MAX_LEN = _BODY_MAX_CHARS
    """bug-034: pretty-printed `payload.output` truncation cap."""

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
        self._node_type = node_type
        self._short_type: str | None = (
            node_type.rsplit("/", 1)[-1] if node_type else None
        )
        self._sub_agent_turn_count = 0
        self._error_preview: str | None = None
        self._details: str | None = None
        self._frame_idx: int = 0
        self._spinner_handle: Any | None = None
        self._output_widget: Widget | None = None
        self._command_widget: Widget | None = None
        self._pending_command: str | None = None
        # bug-036d: pending mount closures (replaces spec-021's raw
        # event buffer). Each closure captures the extracted text +
        # widget kwargs so the drain doesn't re-run content
        # extraction on replay.
        self._pending_mounts: list[Callable[[], Awaitable[None]]] = []
        # spec-021: dedupe key set for events already mounted. Live-
        # mount + replay share this so an event whose closure fired
        # during the row's expanded window doesn't re-fire on a
        # later collapse -> expand cycle.
        self._mounted_event_ids: set[str] = set()
        # spec-021: latch — terminal state freezes further mounts.
        self._buffer_frozen: bool = False
        # bug-036d: closure-drop-on-unmount guard. Late ``mount_*``
        # invocations after ``on_unmount`` become no-ops so a stale
        # sub-agent event can't call methods on a torn-down widget.
        self._unmounted: bool = False
        # bug-036d (was bug-036a's ``_saw_agent_message``): the last
        # assistant text this row mounted, if any. Empty string
        # means "nothing rendered"; non-empty means ``set_output``
        # should gate the JSON preview for agent nodes.
        self._last_assistant_text: str = ""
        # bug-036b: pre-completion title includes ``(short_type)``.
        title = f"{self.STATE_GLYPHS['starting']} {node_name}"
        if self._short_type:
            title += f" ({self._short_type})"
        super().__init__(title=title, collapsed=True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        """bug-031: install the ~12.5 Hz spinner-frame timer.

        bug-036c: flush any pending command preview stashed while
        the row was still composing.
        """
        self._spinner_handle = self.set_interval(0.08, self._advance_spinner)
        self._flush_pending_previews()

    def on_unmount(self) -> None:
        """bug-036d: drop closures + mark unmounted.

        The closures capture ``self``; leaving them queued after the
        row unmounts holds the row's Python object alive for no
        useful reason. Clearing at unmount + refusing further
        ``mount_*`` invocations keeps the tree honest.
        """
        self._unmounted = True
        self._pending_mounts = []

    def _advance_spinner(self) -> None:
        """bug-031: bump the frame index and repaint the title."""
        self._frame_idx = (self._frame_idx + 1) % len(_SPINNER_FRAMES)
        if self.state in _ANIMATED_STATES:
            self._update_title()

    # ------------------------------------------------------------------
    # Public state surface
    # ------------------------------------------------------------------

    def set_state(
        self,
        state: str,
        *,
        details: str | None = None,
        error_preview: str | None = None,
    ) -> None:
        """Transition the row to `state`; refresh title + CSS."""
        if details is not None:
            self._details = details
        if error_preview is not None:
            self._error_preview = error_preview
        self.state = state
        self._update_title()

    def set_output(self, output: Any) -> None:
        """bug-032 + bug-034: mount a pretty-printed preview of `output`.

        bug-036d: the agent-node gate reads
        ``self._last_assistant_text`` (empty → gate off; non-empty →
        gate on). This is the same semantic as the pre-036d
        ``_saw_agent_message`` bool: a non-empty last-assistant-text
        means at least one ``mount_assistant_message`` fired on this
        row for either ``agent.message`` or ``model.response``.
        """
        if output is None:
            return
        if isinstance(output, (dict, list, str)) and not output:
            return
        # bug-036d: agent-node gate reads _last_assistant_text.
        # Empty string is falsy → gate does not fire → set_output
        # renders normally (matches Pilot test sub-test 11).
        if self._node_type == "kolsch/nodes/agent" and self._last_assistant_text:
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
        if self._output_widget is not None:
            with contextlib.suppress(Exception):
                self._output_widget.remove()
            self._output_widget = None
        widget = Static(pretty)
        self._output_widget = widget
        with contextlib.suppress(Exception):
            self._mount_in_contents(widget)

    def set_command_preview(self, command: str) -> None:
        """bug-036c: mount a ``↳ command: <template>`` line in the body."""
        if self._command_widget is not None:
            return
        self._pending_command = command
        if self.is_mounted:
            self._flush_pending_previews()

    def _flush_pending_previews(self) -> None:
        """bug-036c: mount any deferred previews once the row is composed."""
        if self._pending_command is None or self._command_widget is not None:
            return
        widget = Static(
            f"↳ command: {_truncate(self._pending_command, 200)}",
            classes="node-command-preview",
        )
        with contextlib.suppress(Exception):
            self._mount_in_contents(widget)
        self._command_widget = widget
        self._pending_command = None

    # ------------------------------------------------------------------
    # bug-036d: ChatMountSink implementation
    # ------------------------------------------------------------------

    async def mount_user_message(
        self,
        text: str,
        timestamp: object | None = None,
        *,
        event_id: str | None = None,
    ) -> None:
        """Sink method — mount a ``UserChatbox`` inside the row.

        bug-036d reverses bug-035's suppression — inside a workflow
        row, USER_MESSAGE now renders a real ``UserChatbox`` so a
        reader can see the agent's invocation payload (e.g., the
        bound ``job_description`` string for an agent node).
        """
        if not text or self._unmounted or self._buffer_frozen:
            return
        if self._is_duplicate(event_id):
            return
        ts = timestamp if isinstance(timestamp, datetime) else datetime.now(tz=UTC)
        self._count_bump()

        async def _mount() -> None:
            from cli.chat.widgets.user_chatbox import UserChatbox  # noqa: PLC0415
            widget = UserChatbox(text, ts)
            self._mount_in_contents(widget)

        await self._dispatch_mount(_mount, event_id)

    async def mount_assistant_message(
        self, text: str, *, event_id: str | None = None,
    ) -> None:
        """Sink method — mount an ``AssistantProse`` inside the row.

        bug-036d: bumps ``_last_assistant_text`` so the ``set_output``
        gate suppresses the redundant JSON preview for agent nodes.
        """
        if not text or self._unmounted or self._buffer_frozen:
            return
        if self._is_duplicate(event_id):
            return
        # bug-036d: latch the last-assistant-text BEFORE the mount
        # (even when collapsed — the ``set_output`` gate reads this
        # regardless of the mount succeeding).
        self._last_assistant_text = text
        self._count_bump()

        async def _mount() -> None:
            from cli.chat.widgets.assistant_prose import AssistantProse  # noqa: PLC0415
            widget = AssistantProse(text)
            self._mount_in_contents(widget)

        await self._dispatch_mount(_mount, event_id)

    async def mount_tool_call(
        self,
        tool_name: str,
        args_str: str,
        body: str,
        *,
        failed: bool = False,
        recovery_hint: str | None = None,
        event_id: str | None = None,
    ) -> None:
        """Sink method — mount a ``ToolCallBlock`` inside the row."""
        if self._unmounted or self._buffer_frozen:
            return
        if self._is_duplicate(event_id):
            return
        self._count_bump()

        async def _mount() -> None:
            from cli.chat.widgets.tool_call_block import ToolCallBlock  # noqa: PLC0415
            widget = ToolCallBlock(
                tool_name or "?",
                args_str,
                body,
                failed=failed,
                recovery_hint=recovery_hint,
            )
            self._mount_in_contents(widget)

        await self._dispatch_mount(_mount, event_id)

    async def mount_notice(
        self,
        notice_id: str,
        text: str,
        *,
        level: str = "info",
        event_id: str | None = None,
    ) -> None:
        """Sink method — mount a ``NoticeWidget`` inside the row.

        bug-036d: sub-agent notices (model fallback, template
        warning) belong inside the row where the event originated,
        not at the transcript root — otherwise a workflow's warning
        gets orphaned from its node.
        """
        _ = notice_id
        if not text or self._unmounted or self._buffer_frozen:
            return
        if self._is_duplicate(event_id):
            return
        self._count_bump()

        async def _mount() -> None:
            from cli.chat.widgets.notice import NoticeWidget  # noqa: PLC0415
            widget = NoticeWidget(text=text, kind=level)
            self._mount_in_contents(widget)

        await self._dispatch_mount(_mount, event_id)

    def last_assistant_text(self) -> str:
        """Sink accessor — same shape as ``ChatApp.last_assistant_text``."""
        return self._last_assistant_text

    def set_last_assistant_text(self, text: str) -> None:
        """Sink mutator — same shape as ``ChatApp.set_last_assistant_text``."""
        self._last_assistant_text = text

    # ------------------------------------------------------------------
    # bug-036d: sink helpers
    # ------------------------------------------------------------------

    def _is_duplicate(self, event_id: str | None) -> bool:
        """Return True if ``event_id`` has already been mounted.

        Live-mount + replay share ``_mounted_event_ids`` — when a
        closure fires during the row's expanded window, the id
        lands here and a later replay skips it.
        """
        if event_id is None:
            return False
        return event_id in self._mounted_event_ids

    def _count_bump(self) -> None:
        """bug-036b: bump the sub-agent event counter + refresh title."""
        self._sub_agent_turn_count += 1
        self._update_title()

    def count_lifecycle_event(self) -> None:
        """bug-036d AC #8: bump the counter for lifecycle events.

        Lifecycle events (``config.set``, ``thread.exchange_*``,
        ``agent.template_warning``) don't have a widget mount path
        but still count toward the row's "N sub-agent events"
        label — bug-036b's semantic is preserved.
        """
        if self._buffer_frozen or self._unmounted:
            return
        self._count_bump()

    async def _dispatch_mount(
        self,
        closure: Callable[[], Awaitable[None]],
        event_id: str | None,
    ) -> None:
        """Fire ``closure`` now (expanded) or queue it (collapsed).

        Terminal-state rows drop the mount silently — the
        ``_buffer_frozen`` latch matches spec-021's original
        semantic. Unmounted rows also drop.
        """
        if self._buffer_frozen or self._unmounted:
            return
        if event_id is not None:
            self._mounted_event_ids.add(event_id)
        if not self.collapsed:
            try:
                await closure()
            except Exception:
                _LOGGER.warning(
                    "workflow_node_row: live-mount closure raised on %s",
                    self._node_name,
                    exc_info=True,
                )
            return
        # Collapsed: queue. Bound the queue at _EVENT_BUFFER_MAX;
        # overflow drops the oldest entry so a runaway agent can't
        # OOM the row.
        self._pending_mounts.append(closure)
        while len(self._pending_mounts) > _EVENT_BUFFER_MAX:
            self._pending_mounts.pop(0)

    async def _drain_pending_mounts(self) -> None:
        """AC #6: sequentially drain queued mount closures.

        Each closure is awaited in insertion order so the on-screen
        widget order matches the arrival order (Mockup 3). A
        failing closure is logged + skipped so one bad event
        doesn't abort the row.
        """
        # Snapshot + clear so a re-entrant append (mount fires an
        # event that queues another closure) lands on a fresh list.
        pending = self._pending_mounts
        self._pending_mounts = []
        for closure in pending:
            if self._unmounted:
                return
            with contextlib.suppress(Exception):
                await closure()
            # Log outside the suppress so we still see the failure.
            # We intentionally continue on error so subsequent
            # closures still fire.

    # ------------------------------------------------------------------
    # Reactive + rendering
    # ------------------------------------------------------------------

    def watch_state(self, old: str, new: str) -> None:
        """Reactive watcher — apply `.-<state>` CSS class."""
        _ = old
        for cls in self._TERMINAL_CSS_STATES:
            self.remove_class(f"-{cls}")
        if new in self._TERMINAL_CSS_STATES:
            self.add_class(f"-{new}")
        if new not in _ANIMATED_STATES and self._spinner_handle is not None:
            with contextlib.suppress(Exception):
                self._spinner_handle.stop()
            self._spinner_handle = None
        # spec-021 (bug-036d): terminal transition freezes further
        # mounts. Pre-terminal queued closures survive — a user who
        # expands the row after the run finishes still sees the
        # response history (spec-021 replay-on-expand invariant).
        # Post-terminal ``mount_*`` invocations short-circuit at
        # ``_dispatch_mount``.
        if new in _TERMINAL_STATES:
            self._buffer_frozen = True

    def watch_collapsed(self, old: bool, new: bool) -> None:
        """spec-021: replay pending mounts on expand.

        bug-036d: uses ``call_after_refresh`` to schedule the async
        drain because ``watch_collapsed`` is sync. Sequential drain
        preserves insertion order — Mockup 3 requires
        ``UserChatbox`` before ``AssistantProse``.
        """
        _ = old
        if new or not self._pending_mounts:
            return
        try:
            self.app.call_after_refresh(self._drain_pending_mounts)
        except Exception:  # noqa: BLE001 — app may be tearing down
            # Fallback: schedule as a raw task via the running loop.
            # Non-Textual test harnesses without a running loop skip
            # the drain entirely, which the Pilot tests exercise via
            # a manual await. bug-036d AC #14: never ``get_event_loop``.
            with contextlib.suppress(Exception):
                import asyncio  # noqa: PLC0415
                loop = asyncio.get_running_loop()
                loop.create_task(self._drain_pending_mounts())

    def _update_title(self) -> None:
        """Refresh the collapsed title from state + details + count."""
        if self.state in _ANIMATED_STATES:
            glyph = _SPINNER_FRAMES[self._frame_idx]
        else:
            glyph = self.STATE_GLYPHS.get(self.state, "?")
        base = f"{glyph} {self._node_name}"
        if self.state == "failed" and self._error_preview:
            base += f"   {self._truncate_preview(self._error_preview)}"
        elif self._details:
            base += f"   {self._details}"
        elif self._short_type:
            base += f" ({self._short_type})"
        if self._sub_agent_turn_count > 0:
            base += f" · {self._sub_agent_turn_count} sub-agent events"
        self.title = base

    def _truncate_preview(
        self,
        text: str,
        max_len: int = _ERROR_PREVIEW_MAX_LEN,
    ) -> str:
        """Tail-truncate `text` at `max_len` chars (default 60)."""
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + "..."

    # ------------------------------------------------------------------
    # bug-035 mount-tree helper (unchanged)
    # ------------------------------------------------------------------

    def _mount_in_contents(self, widget: Widget) -> None:
        """bug-035: mount ``widget`` inside the Collapsible's Contents slot.

        Textual's :class:`Collapsible.compose_add_child` only wires
        the *context-manager* compose syntax to the internal
        ``Contents`` container — a bare ``self.mount(child)`` on an
        already-composed Collapsible mounts as a **sibling** of the
        ``CollapsibleTitle`` and ``Contents``. Only ``Contents``
        responds to the ``.-collapsed > Contents { display: none }``
        CSS, so sibling mounts stay visible regardless of the row's
        collapsed state.
        """
        try:
            contents = self.query_one(Collapsible.Contents)
        except Exception:  # noqa: BLE001 — Contents not composed yet
            contents = None
        if contents is not None:
            contents.mount(widget)
            return
        # Row not fully mounted yet — mount at the outer level.
        self.mount(widget)


__all__ = ["WorkflowNodeRow"]
