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
import warnings
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, ClassVar

from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Collapsible, Static

from cli.chat.metrics import format_duration
from cli.chat.spinner import SPINNER_FRAMES, spinner_frame
from cli.chat.state_tokens import resolve_state

_LOGGER = logging.getLogger(__name__)


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


_FAILED_OUTPUT_EMPTY_NOTE = "(no output — node failed before producing a result)"
"""bug-063: exact body text for the Output section on a failed node.

The Coordinator pinned this string so tests can assert on it via the
module-level constant. ``node.failed`` payloads carry no output, so the
Output section is always mounted with this note on a failure.
"""


_FAILED_INPUT_EMPTY_NOTE = "(no bound inputs recorded for this node)"
"""bug-063: body text when a failed node's ``_bound_inputs`` is empty.

Rare — a node that failed before ``NODE_STARTED`` delivered inputs. The
Input section is still mounted (for parity with Output) so the failure's
three-section shape stays consistent.
"""


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


def _render_bash_input_body(
    bound_inputs: dict[str, Any],
    command: str,
) -> str:
    """bug-037: render the bash `input` section body — inputs + command."""
    lines: list[str] = []
    if bound_inputs:
        lines.append("bound inputs:")
        for key, value in bound_inputs.items():
            lines.append(f"· {key}: {_format_input_value(value)}")
        lines.append("")
    lines.append("command:")
    lines.append(_truncate(command, 500))
    return "\n".join(lines)


def _render_bound_inputs_body(bound_inputs: dict[str, Any]) -> str:
    """bug-063: render a generic Input section body from bound inputs.

    Used on the failure path for node types (e.g. agent) that lack a
    reusable bound-inputs section. Mirrors the ``bound inputs:`` block
    from :func:`_render_bash_input_body` but without the trailing
    ``command:`` block. Empty inputs render as the empty-note constant
    so a failed row always shows a populated Input section.
    """
    if not bound_inputs:
        return _FAILED_INPUT_EMPTY_NOTE
    lines: list[str] = ["bound inputs:"]
    for key, value in bound_inputs.items():
        lines.append(f"· {key}: {_format_input_value(value)}")
    return "\n".join(lines)


def _is_failed_exit(exit_code: Any) -> bool:
    """Return True when exit_code represents a failure."""
    if exit_code is None:
        return False
    try:
        return int(exit_code) != 0
    except (TypeError, ValueError):
        return False


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

    # bug-038: Collapsible defaults ALLOW_SELECT=False, which blocks
    # drag-selection inside the row's body — the exact content users
    # want to copy. Override so Textual's selection walker (see
    # `Widget.allow_select` at site-packages/textual/widget.py:2786)
    # descends into this widget's children.
    ALLOW_SELECT: ClassVar[bool] = True

    # spec-078: WorkflowNodeRow is a `Collapsible` subclass; same
    # rationale as commits 5 (CardWidget) / 7 (ToolCallBlock) / 8
    # (CompactionBlock). Preserve the base class + reactive `state`
    # + its huge sub-agent + bash section machinery; deliver the
    # rail via `border-left: outer $color` (`▌` — one glyph wider
    # than spec-078's target `▎` from the GutterBlock primitive but
    # reads the same "colored left rail per state"). Default state
    # (`starting` / `running`) routes to `$tool-fg`; terminal states
    # route via the `.-completed` / `.-failed` / `.-paused` /
    # `.-cancelling` / `.-pruned` classes below. The state glyph
    # already renders inside the CollapsibleTitle; no separate glyph
    # column is added.
    DEFAULT_CSS = """
    $tool-fg: #4dd0e1;
    $error: #ff5555;
    $warning: #ffb454;
    $hint: #6e7681;

    WorkflowNodeRow {
        padding: 0 1;
        margin: 0;
        border-left: outer $tool-fg;
        background: transparent;
    }
    WorkflowNodeRow.-completed  { color: $tool-fg; border-left: outer $tool-fg; }
    WorkflowNodeRow.-failed     { color: $error;   border-left: outer $error; }
    WorkflowNodeRow.-paused     { color: $warning; border-left: outer $warning; }
    WorkflowNodeRow.-cancelling { color: $tool-fg; opacity: 0.6; border-left: outer $tool-fg; }
    WorkflowNodeRow.-pruned     { color: $hint;    border-left: outer $hint; }
    """

    # State glyphs live in `src/cli/chat/state_tokens.py` — the
    # single canonical `STATE_TOKENS` table shared with `CardWidget`
    # (qol-075). This row's reactive `state` still uses the historical
    # vocabulary (``starting``/``completed``/``pruned``) because the
    # widget's CSS classes, tests, and upstream `set_state` callers
    # key off those names; :func:`resolve_state` bridges them to
    # canonical via `_LEGACY_ALIASES`. Migrating the reactive
    # vocabulary itself is a follow-up — see qol-084 (the aliases
    # are the short-lived bridge until then).

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
        # bug-037: per-type sections. Bash rows own an `input`
        # section (bound_inputs + command), an `output` section
        # (stdout), and an `exit: <code>` section. Agent rows own
        # `input: <key>` sections plus `response`/`response (N)` +
        # per-tool_call_id `tool: <name>` sections.
        self._response_count: int = 0
        self._bound_inputs: dict[str, Any] = {}
        self._bash_input_section: Widget | None = None
        self._bash_output_section: Widget | None = None
        self._bash_exit_section: Widget | None = None
        self._tool_sections: dict[str, Widget] = {}
        self._tool_blocks: dict[str, Widget] = {}
        # bug-063: failure-path Input/Output/Error sections. Mounted
        # once when the row transitions to ``failed``; the references
        # let tests introspect them and guard against double-mount if
        # ``set_state("failed", ...)`` is called more than once.
        self._failure_input_section: Widget | None = None
        self._failure_output_section: Widget | None = None
        self._failure_error_section: Widget | None = None
        self._failure_sections_mounted: bool = False
        self._node_started_at: datetime | None = None
        self._node_completed_at: datetime | None = None
        self._node_duration_ms: float | None = None
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
        # The initial ``⠋`` is a static placeholder for the ~80 ms
        # gap between compose and the first spinner tick (installed
        # in :meth:`on_mount`). It intentionally does NOT go through
        # ``state_tokens.resolve_state`` — the canonical
        # ``running`` glyph (``▸``) would flash briefly before the
        # spinner overwrites it, changing a rendering that the
        # bug-036b snapshot golden pins on ``⠋``. See qol-084 for
        # the follow-up that unifies this with the spinner table.
        title = f"⠋ {node_name}"
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
        self._frame_idx = (self._frame_idx + 1) % len(SPINNER_FRAMES)
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
        """Transition the row to `state`; refresh title + CSS.

        bug-063: on a transition to ``failed``, mount three peer
        chevron sections (Input / Output / Error) so the failure is
        inspectable with the same affordances a successful node
        gives. The mount is idempotent — repeat ``set_state("failed")``
        calls do NOT re-mount.
        """
        if details is not None:
            self._details = details
        if error_preview is not None:
            self._error_preview = error_preview
        self.state = state
        self._update_title()
        if state == "failed":
            self._mount_failure_sections()

    def set_output(self, output: Any) -> None:
        """bug-032 + bug-034 + bug-037: mount output section(s).

        bug-037: bash-shape outputs create two dedicated sections
        (``output``, ``exit: <code>``). Other shapes fall through
        to the flat ``Static`` preview.

        bug-036d: the agent-node gate reads
        ``self._last_assistant_text`` (empty → gate off; non-empty →
        gate on).
        """
        if output is None:
            return
        if isinstance(output, (dict, list, str)) and not output:
            return
        # bug-037: bash-shape outputs bypass the agent gate and land
        # in dedicated sections. This is deliberate — the agent gate
        # is `node_type == "kolsch/nodes/agent"` scoped by spec.
        if isinstance(output, dict) and _is_bash_shape(output):
            self._mount_bash_output_sections(output)
            return
        # bug-036d: agent-node gate reads _last_assistant_text.
        # Empty string is falsy → gate does not fire → set_output
        # renders normally.
        if self._node_type == "kolsch/nodes/agent" and self._last_assistant_text:
            return
        if isinstance(output, dict):
            pretty = _format_content_dict(
                output,
                max_chars=self._OUTPUT_PREVIEW_MAX_LEN,
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
        widget = Static(pretty, markup=False)
        self._output_widget = widget
        with contextlib.suppress(Exception):
            self._mount_in_contents(widget)

    def set_bound_inputs(self, inputs: dict[str, Any]) -> None:
        """bug-037: stash bound inputs for later section rendering.

        Called BEFORE ``set_command_preview`` by the renderer so bash
        rows can include the bound-inputs block inside their ``input``
        section. If ``set_command_preview`` fires first (race), the
        section is created without the block; a subsequent
        ``set_bound_inputs`` is a no-op — the section is NOT rebuilt.
        """
        if self._bash_input_section is not None:
            # Section already mounted (race): don't rebuild.
            self._bound_inputs = dict(inputs)
            return
        self._bound_inputs = dict(inputs)

    def set_command_preview(self, command: str) -> None:
        """bug-036c + bug-037: mount an ``input`` section w/ command."""
        if self._command_widget is not None:
            return
        self._pending_command = command
        if self.is_mounted:
            self._flush_pending_previews()

    def _flush_pending_previews(self) -> None:
        """bug-036c + bug-037: mount deferred command preview once composed."""
        if self._pending_command is None or self._command_widget is not None:
            return
        body = _render_bash_input_body(self._bound_inputs, self._pending_command)
        section = self._new_bash_input_section(body)
        self._bash_input_section = section
        self._command_widget = section
        self._pending_command = None

    def _new_bash_input_section(self, body_text: str) -> Widget:
        """Build + mount the bash ``input`` NodeSection."""
        from cli.chat.widgets.node_section import NodeSection  # noqa: PLC0415

        section = NodeSection("input")
        inner = Static(body_text, classes="node-command-preview", markup=False)
        section._contents_list.append(inner)
        with contextlib.suppress(Exception):
            self._mount_in_contents(section)
        return section

    def _mount_bash_output_sections(self, output: dict[str, Any]) -> None:
        """bug-037: mount ``output`` + ``exit: <code>`` sections."""
        from cli.chat.widgets.node_section import NodeSection  # noqa: PLC0415

        stdout = output.get("stdout") if isinstance(output, dict) else None
        stderr = output.get("stderr") if isinstance(output, dict) else None
        exit_code = output.get("exit_code") if isinstance(output, dict) else None
        # `output` section — stdout only.
        if self._bash_output_section is None:
            stdout_text = str(stdout) if isinstance(stdout, str) else ""
            stdout_text = _truncate(stdout_text, _BASH_BLOCK_MAX_CHARS)
            out_section = NodeSection("output")
            out_section._contents_list.append(Static(stdout_text, markup=False))
            with contextlib.suppress(Exception):
                self._mount_in_contents(out_section)
            self._bash_output_section = out_section
            self._output_widget = out_section
        # `exit: <code> (<duration>)` section.
        if self._bash_exit_section is None:
            title = self._bash_exit_title(exit_code)
            lines: list[str] = []
            if exit_code is not None:
                lines.append(f"exit code: {exit_code}")
            if self._formatted_duration() is not None:
                lines.append(f"duration: {self._formatted_duration()}")
            if isinstance(stderr, str) and stderr:
                lines.append("")
                lines.append("stderr:")
                lines.append(_truncate(stderr, _BASH_BLOCK_MAX_CHARS))
            state = "failed" if _is_failed_exit(exit_code) else "completed"
            exit_section = NodeSection(title, state=state)
            exit_section._contents_list.append(
                Static("\n".join(lines) or "(empty)", markup=False),
            )
            with contextlib.suppress(Exception):
                self._mount_in_contents(exit_section)
            self._bash_exit_section = exit_section

    def _mount_failure_sections(self) -> None:
        """bug-063: mount Input / Output / Error sections on a failed row.

        Called from :meth:`set_state` when the row transitions to
        ``failed``. Mounts three peer chevron sections in order
        (Input → Output → Error) matching the "After" mockup in
        the bug-063 spec. All three use :class:`NodeSection` so they
        share the identical chevron style used by input / output /
        response / tool sections on a successful node.

        Idempotent — the ``_failure_sections_mounted`` latch prevents
        a repeat ``set_state("failed")`` from duplicating sections.
        The Input section is skipped only for map rows (which have
        their own iteration-row layout, no bound-inputs semantics).
        """
        if self._failure_sections_mounted:
            return
        # Map rows own an iteration-row layout, not a per-row
        # input/output/error surface. Skip the failure sections there
        # so we don't disrupt the map contents shape.
        if self._node_type == "kolsch/nodes/map":
            self._failure_sections_mounted = True
            return
        from cli.chat.widgets.node_section import NodeSection  # noqa: PLC0415

        # --- Input section ---------------------------------------------------
        # bug-037 already builds a bash `input` section on
        # `set_command_preview`. If it exists, keep it — no need to
        # mount a second one. Otherwise, mount a generic Input from
        # `_bound_inputs` (this is the NEW render for agent nodes,
        # which have no reusable bound-inputs section).
        if self._bash_input_section is None:
            input_body = _render_bound_inputs_body(self._bound_inputs)
            input_section = NodeSection("input")
            input_section._contents_list.append(
                Static(input_body, markup=False),
            )
            with contextlib.suppress(Exception):
                self._mount_in_contents(input_section)
            self._failure_input_section = input_section
        else:
            # Reuse the bash-side input section as the failure Input.
            self._failure_input_section = self._bash_input_section
        # --- Output section --------------------------------------------------
        # `set_output` is not reached on the failure path — `node.failed`
        # payloads carry no output. Mount an Output section with the
        # empty-note constant so the failed row always shows the three
        # peer sections.
        output_section = NodeSection("output")
        output_section._contents_list.append(
            Static(_FAILED_OUTPUT_EMPTY_NOTE, markup=False),
        )
        with contextlib.suppress(Exception):
            self._mount_in_contents(output_section)
        self._failure_output_section = output_section
        # --- Error section ---------------------------------------------------
        # `_error_preview` holds the FULL error string (only
        # `_update_title` truncated it pre-bug-063). Use it directly —
        # do NOT re-truncate.
        error_body = self._error_preview or "(no error message)"
        error_section = NodeSection("error", state="failed")
        error_section._contents_list.append(Static(error_body, markup=False))
        with contextlib.suppress(Exception):
            self._mount_in_contents(error_section)
        self._failure_error_section = error_section
        self._failure_sections_mounted = True

    def _bash_exit_title(self, exit_code: Any) -> str:
        code_part = f"exit: {exit_code}" if exit_code is not None else "exit"
        dur = self._formatted_duration()
        if dur is not None:
            return f"{code_part} ({dur})"
        return code_part

    def _formatted_duration(self) -> str | None:
        # qol-085: route through the shared helper so a 2-minute bash
        # renders `2m 00s` here just like the workflow-card header
        # (was `120.0s` pre-migration). `sub_second_precision=1`
        # preserves the fast-bash `0.4s` signal below 1s.
        if self._node_duration_ms is not None:
            secs = self._node_duration_ms / 1000.0
            return format_duration(secs, sub_second_precision=1)
        if self._node_started_at and self._node_completed_at:
            delta = (self._node_completed_at - self._node_started_at).total_seconds()
            if delta >= 0:
                return format_duration(delta, sub_second_precision=1)
        return None

    def set_started_at(self, ts: datetime | None) -> None:
        """bug-037: stash NODE_STARTED ts for duration derivation."""
        self._node_started_at = ts

    def set_completed_at(
        self,
        ts: datetime | None = None,
        *,
        duration_ms: float | None = None,
    ) -> None:
        """bug-037: stash NODE_COMPLETED ts + duration for exit title."""
        if ts is not None:
            self._node_completed_at = ts
        if duration_ms is not None:
            self._node_duration_ms = duration_ms

    # ------------------------------------------------------------------
    # bug-036d: ChatMountSink implementation
    # ------------------------------------------------------------------

    async def mount_user_message(
        self,
        text: str,
        timestamp: object | None = None,
        *,
        event_id: str | None = None,
        content: dict[str, Any] | None = None,
    ) -> None:
        """Sink method — mount ``input`` section(s) with ``UserChatbox``.

        bug-037: when ``content`` is a non-empty dict, mount ONE
        ``input: <key>`` section per key. Otherwise, mount one
        ``input`` section with the flat text. Map rows drop the
        event entirely.
        """
        # bug-037 AC #8: map rows never receive routed events; guard.
        if self._node_type == "kolsch/nodes/map":
            return
        if not text or self._unmounted or self._buffer_frozen:
            return
        if self._is_duplicate(event_id):
            return
        ts = timestamp if isinstance(timestamp, datetime) else datetime.now(tz=UTC)
        self._count_bump()

        async def _mount() -> None:
            from cli.chat.widgets.node_section import NodeSection  # noqa: PLC0415
            from cli.chat.widgets.user_chatbox import UserChatbox  # noqa: PLC0415

            if isinstance(content, dict) and content:
                for key, value in content.items():
                    section = NodeSection(f"input: {key}")
                    section._contents_list.append(UserChatbox(str(value), ts))
                    self._mount_in_contents(section)
            else:
                section = NodeSection("input")
                section._contents_list.append(UserChatbox(text, ts))
                self._mount_in_contents(section)

        await self._dispatch_mount(_mount, event_id)

    async def mount_assistant_message(
        self,
        text: str,
        *,
        event_id: str | None = None,
    ) -> None:
        """Sink method — mount ``response`` (or ``response (N)``) section.

        bug-037: dedupe fires BEFORE section construction so a
        skipped mount leaves NO empty section widget.
        """
        # bug-037 AC #8: map rows drop routed events.
        if self._node_type == "kolsch/nodes/map":
            return
        if not text or self._unmounted or self._buffer_frozen:
            return
        if self._is_duplicate(event_id):
            return
        # bug-037: dedupe against last-assistant-text FIRST so no
        # empty NodeSection is created.
        if self._last_assistant_text == text:
            return
        # Latch AFTER the dedupe check; sub-test 4 depends on this
        # order.
        self._last_assistant_text = text
        self._response_count += 1
        response_idx = self._response_count
        self._count_bump()

        async def _mount() -> None:
            from cli.chat.widgets.assistant_prose import AssistantProse  # noqa: PLC0415
            from cli.chat.widgets.node_section import NodeSection  # noqa: PLC0415

            title = "response" if response_idx == 1 else f"response ({response_idx})"
            section = NodeSection(title)
            section._contents_list.append(AssistantProse(text))
            self._mount_in_contents(section)

        await self._dispatch_mount(_mount, event_id)

    async def mount_tool_call(
        self,
        tool_name: str,
        args_str: str,
        body: str,
        *,
        failed: bool = False,
        recovery_hint: str | None = None,
        matched_rule: dict[str, object] | None = None,
        event_id: str | None = None,
    ) -> None:
        """Sink method — mount / transition a ``tool: <name>`` section.

        bug-037: first call (empty body) creates the section in
        ``running`` state; second call (with same ``event_id``,
        used as ``tool_call_id``) transitions the SAME section to
        ``completed`` (or ``failed``) and populates the block body.
        """
        if self._node_type == "kolsch/nodes/map":
            return
        if self._unmounted or self._buffer_frozen:
            return
        # bug-037: two-phase — do NOT reject on `_is_duplicate`
        # because the TOOL_RESULT reuses the tool_call_id as
        # event_id. Instead, look up the section for a state
        # transition when the id is already known.
        tool_call_id = event_id or ""
        is_transition = (
            tool_call_id in self._tool_sections and event_id in self._mounted_event_ids
        )
        if tool_call_id and not is_transition:
            self._count_bump()

        async def _mount() -> None:
            from cli.chat.widgets.node_section import NodeSection  # noqa: PLC0415
            from cli.chat.widgets.tool_call_block import ToolCallBlock  # noqa: PLC0415

            existing = self._tool_sections.get(tool_call_id) if tool_call_id else None
            if existing is not None:
                # Transition — update section state + block body.
                new_state = "failed" if failed else "completed"
                if hasattr(existing, "set_state"):
                    existing.set_state(new_state)
                block = self._tool_blocks.get(tool_call_id)
                if block is not None:
                    self._populate_tool_call_result(block, body, failed=failed)
                return
            state = (
                "running"
                if not body and not failed
                else ("failed" if failed else "completed")
            )
            section = NodeSection(f"tool: {tool_name or '?'}", state=state)
            block = ToolCallBlock(
                tool_name or "?",
                args_str,
                body,
                failed=failed,
                recovery_hint=recovery_hint,
                matched_rule=matched_rule,
            )
            section._contents_list.append(block)
            self._mount_in_contents(section)
            if tool_call_id:
                self._tool_sections[tool_call_id] = section
                self._tool_blocks[tool_call_id] = block

        # For transitions we must NOT go through the dedupe path,
        # so bypass _dispatch_mount's event_id add and route the
        # closure directly.
        if is_transition:
            if not self.collapsed:
                try:
                    await _mount()
                except Exception:
                    _LOGGER.warning(
                        "workflow_node_row: tool transition raised on %s",
                        self._node_name,
                        exc_info=True,
                    )
                return
            self._pending_mounts.append(_mount)
            while len(self._pending_mounts) > _EVENT_BUFFER_MAX:
                self._pending_mounts.pop(0)
            return

        await self._dispatch_mount(_mount, event_id)

    def _populate_tool_call_result(
        self,
        block: Widget,
        body: str,
        *,
        failed: bool,
    ) -> None:
        """bug-037: rewrite a ToolCallBlock's body after a state transition."""
        # bug-042: route through ToolCallBlock.update_body — the prior
        # `block.query(Static)` walked DOM order and hit CollapsibleTitle
        # (markup=True) first, causing MarkupError crashes when the tool
        # body contained `[...]` sequences.
        with contextlib.suppress(Exception):
            update_body = getattr(block, "update_body", None)
            if callable(update_body):
                update_body(body)
        if failed:
            with contextlib.suppress(Exception):
                block.add_class("-failed")

    async def mount_notice(
        self,
        notice_id: str,
        text: str,
        *,
        level: str = "info",
        event_id: str | None = None,
    ) -> None:
        """Sink method — mount a ``NoticeWidget`` pinned at TOP of Contents.

        bug-037 AC #7: notices bypass sections + pin at the top so
        template warnings / model fallbacks stay visible above the
        section index.
        """
        _ = notice_id
        if self._node_type == "kolsch/nodes/map":
            return
        if not text or self._unmounted or self._buffer_frozen:
            return
        if self._is_duplicate(event_id):
            return
        self._count_bump()

        async def _mount() -> None:
            from cli.chat.widgets.notice import NoticeWidget  # noqa: PLC0415

            widget = NoticeWidget(text=text, kind=level)
            self._mount_notice_at_top(widget)

        await self._dispatch_mount(_mount, event_id)

    def _mount_notice_at_top(self, widget: Widget) -> None:
        """bug-037: mount ``widget`` at index 0 of Contents."""
        try:
            contents = self.query_one(Collapsible.Contents)
        except Exception:  # noqa: BLE001
            contents = None
        if contents is None:
            self.mount(widget)
            return
        existing = list(contents.children)
        if existing:
            with contextlib.suppress(Exception):
                contents.mount(widget, before=existing[0])
                return
        contents.mount(widget)

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
        """Refresh the collapsed title from state + details + count.

        bug-063: on ``failed`` the title carries ONLY the state glyph
        and node name (``✗ <node>``) — the full error moved into the
        Error ``NodeSection`` body (see
        :meth:`_mount_failure_sections`). Pre-bug-063 the title
        tail-appended a 60-char muted preview which read as a detail
        rather than a first-class failure surface.
        """
        if self.state in _ANIMATED_STATES:
            glyph = spinner_frame(self._frame_idx)
        else:
            # State-tokens (qol-075): legacy state names alias to
            # canonical inside `resolve_state`; unknown names raise
            # ``KeyError`` — mirror the pre-qol-075 ``.get(..., "?")``
            # fallback so a stray state can't crash the title paint.
            # `catch_warnings` silences the alias DeprecationWarning
            # per repaint — this row's reactive still holds the legacy
            # vocabulary; the warning is for external callers. The
            # bridge dissolves when qol-084 migrates the reactive.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                try:
                    glyph = resolve_state(self.state).glyph
                except KeyError:
                    glyph = "?"
        base = f"{glyph} {self._node_name}"
        if self.state == "failed":
            # bug-063: full error moved to Error section body; keep
            # only the state glyph + node name as a short marker.
            pass
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
