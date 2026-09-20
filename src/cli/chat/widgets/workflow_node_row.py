# ruff: noqa: E501 — module docstring holds an atomic mkdocs cross-ref that
# must stay on one line for autorefs to resolve; unsplittable.
"""`WorkflowNodeRow` — per-node collapsible row inside a workflow card.

spec-013 §WorkflowNodeRow. Mounted inside a
[`WorkflowNodeList`][cli.chat.widgets.card_body.workflow.WorkflowNodeList]
by [`WorkflowRenderer.apply_event`][cli.chat.widgets.card_body.workflow.WorkflowRenderer.apply_event]
on `workflow.node.started`. The row is a
`Collapsible` with a reactive
`state` field driving CSS class + glyph.

State machine (6 canonical states — qol-084 migrated the historical
7-state vocabulary onto the shared canonical set; the pre-qol-084
``starting`` and ``running`` collapsed into a single ``running``
that carries both the freshly-mounted spinner window and the
observed sub-agent activity):

- `running` (`▸`) — freshly mounted or sub-agent activity
  observed. Animated: the spinner-frame table overrides the
  canonical `▸` while the row is mid-execution.
- `success` (`✓`) — terminal success, class `.-success`.
- `failed` (`✗`) — terminal failure, class `.-failed`; the
  collapsed title carries an inline error preview truncated to
  60 chars (Coordinator #4).
- `paused` (`⚠`) — awaiting approval (spec-014 wires the trigger),
  class `.-paused`.
- `cancelling` (`⧗`) — run cancel fired, node unwinding, class
  `.-cancelling`.
- `cancelled` (`─`) — node never ran (branch cascade), class
  `.-cancelled`.

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
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from textual.reactive import reactive
from textual.widgets import Collapsible, Static

from cli.chat.metrics import format_duration
from cli.chat.spinner import SPINNER_FRAMES, spinner_frame
from cli.chat.state_tokens import resolve_state
from cli.chat.widgets.assistant_prose import WorkflowAnchorButton

if TYPE_CHECKING:
    from textual.widget import Widget

    from cli.chat.widgets.node_section import NodeSection

_LOGGER = logging.getLogger(__name__)


_ANIMATED_STATES = frozenset({"running"})
"""bug-031: states whose title uses the spinner frame.

qol-084 collapsed the pre-migration ``starting``/``running`` pair
into a single canonical ``running`` — both meanings now share the
one animated state.
"""


_EVENT_BUFFER_MAX = 50
"""spec-021: bounded queue capacity for pending mount closures.

bug-036d renames the concept from "event buffer" to "pending
mounts" but keeps the 50-slot bound so a collapsed row can't
grow unbounded while a sub-agent runs.
"""


_TERMINAL_STATES = frozenset(
    {"success", "failed", "cancelling", "cancelled", "skipped"}
)
"""spec-021: states that freeze the pending-mount queue.

bug-096: ``skipped`` (a branch-pruned node that never ran) joins the
freeze set — a node the executor pruned will never produce sub-agent
mounts, so freezing its buffer is correct. ``pending`` is
deliberately NOT here: a placeholder must still accept mounts once it
adopts on NODE_STARTED, so its buffer stays live.
"""


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


_CAP_MAX_CHARS = 20_000
"""qol-108: soft ceiling on content/command body chars.

Applied to write_file ``content`` and bash ``command`` bodies (both of
which can be arbitrarily long). Char-count (not bytes) — matches
``len(str)`` semantics natively and avoids UTF-8 multi-byte edge cases
in the truncation-marker assertions. Shared with qol-109, which passes
``max_chars=16_000`` explicitly to `_cap_with_tail` for its own body
sizing.
"""


_CAP_MARKER_TEMPLATE = "\n… ({n_more} more chars; see session JSONL)"
"""qol-108: tail marker appended by :func:`_cap_with_tail` on truncation.

Pinned as a module-level template so Test 5's substring assertion has a
stable contract; ``n_more`` = ``len(text) - max_chars``.
"""


def _cap_with_tail(text: str, max_chars: int = _CAP_MAX_CHARS) -> str:
    """qol-108: char-cap ``text`` at ``max_chars`` with a JSONL-tail marker.

    Shared helper for the write_file ``content`` and bash ``command`` body
    sizing. Under the cap → passthrough. Over the cap → the first
    ``max_chars`` chars followed by ``_CAP_MARKER_TEMPLATE`` (populated
    with the number of chars dropped). Callers keep the truncated body
    visible in the row while the durable session JSONL retains the full
    text.
    """
    if len(text) <= max_chars:
        return text
    n_more = len(text) - max_chars
    return text[:max_chars] + _CAP_MARKER_TEMPLATE.format(n_more=n_more)


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


def _render_workflow_inputs_used_block(
    workflow_inputs_used: dict[str, Any] | None,
) -> list[str]:
    """bug-094: render the ``workflow inputs used:`` sub-block lines.

    Empty / ``None`` → no lines. Otherwise a labelled block mirroring the
    ``bound inputs:`` per-key formatting, so the user can see WHICH
    workflow-scope inputs this node's templated config referenced (a
    distinct source from the inbound-edge binds shown by ``bound inputs:``).
    """
    if not workflow_inputs_used:
        return []
    lines = ["workflow inputs used:"]
    for key, value in workflow_inputs_used.items():
        lines.append(f"· {key}: {_format_input_value(value)}")
    return lines


def _render_command_block(command: str, rendered: str | None) -> list[str]:
    """qol-108 (was bug-036c + bug-094): the ``▶ command`` section body lines.

    Post-qol-108 the bash ``command`` block owns its own top-level
    ``NodeSection`` (title ``command``); the body no longer carries a
    redundant ``command:`` label. Layout:

    - ``rendered is None`` OR ``rendered == command`` → single body of the
      command text, capped via :func:`_cap_with_tail` (~20 000 chars).
    - ``rendered != command`` → rendered form (capped via
      :func:`_cap_with_tail`) as the primary body, followed by a
      ``template:`` sub-block whose value stays capped at 500 chars
      (short-form context, not the primary body).
    """
    lines: list[str] = []
    if rendered is not None and rendered != command:
        lines.append(_cap_with_tail(rendered))
        lines.append("template:")
        lines.append(_truncate(command, 500))
    else:
        lines.append(_cap_with_tail(command))
    return lines


def _render_path_body(path: str, rendered: str | None) -> str:
    """qol-108: body text for a write_file ``▶ path`` section.

    Paths are short-form (a filesystem path), so the primary body stays
    capped at 500 chars. When ``rendered`` differs from the raw ``path``
    template, the rendered form is the primary line and a ``template:``
    sub-block carries the raw (also 500-cap — short-form context).
    """
    if rendered is not None and rendered != path:
        return "\n".join(
            [
                _truncate(rendered, 500),
                "template:",
                _truncate(path, 500),
            ]
        )
    return _truncate(path, 500)


def _render_content_body(content: str, rendered: str | None) -> str:
    """qol-108: body text for a write_file ``▶ content`` section.

    Content can be arbitrarily long — the primary body is capped via
    :func:`_cap_with_tail` (~20 000 chars with a JSONL-tail marker).
    ``template:`` sub-block (when raw != rendered) stays at 500 chars
    per the rendered-vs-template convention (short-form context).
    """
    return "\n".join(_render_command_block(content, rendered))


def _render_bash_binds_body(
    bound_inputs: dict[str, Any],
    *,
    workflow_inputs_used: dict[str, Any] | None = None,
) -> str:
    """qol-108 (was ``_render_bash_input_body``): the bash ``▶ input`` body.

    Post-qol-108 the ``command:`` block lives in its own top-level
    ``▶ command`` section; the ``▶ input`` section on a bash row now
    carries ONLY the binds surface (``bound inputs:`` + bug-094's
    ``workflow inputs used:``). Each block renders only when non-empty;
    both empty → empty string (the caller suppresses the mount, per
    bug-037's empty-block-suppressed convention).
    """
    lines: list[str] = []
    if bound_inputs:
        lines.append("bound inputs:")
        for key, value in bound_inputs.items():
            lines.append(f"· {key}: {_format_input_value(value)}")
    used_lines = _render_workflow_inputs_used_block(workflow_inputs_used)
    if used_lines:
        if lines:
            lines.append("")
        lines.extend(used_lines)
    return "\n".join(lines)


def _render_bound_inputs_body(
    bound_inputs: dict[str, Any],
    *,
    workflow_inputs_used: dict[str, Any] | None = None,
) -> str:
    """bug-063 + bug-094: render a generic Input section body from bound inputs.

    Used on the failure path for node types (e.g. agent) that lack a
    reusable bound-inputs section. Mirrors the ``bound inputs:`` block
    from :func:`_render_bash_binds_body` (plus the bug-094 ``workflow
    inputs used:`` sub-block). Empty inputs (and no workflow-inputs-used)
    render as the empty-note constant so a failed row always shows a
    populated Input section.
    """
    used_lines = _render_workflow_inputs_used_block(workflow_inputs_used)
    if not bound_inputs and not used_lines:
        return _FAILED_INPUT_EMPTY_NOTE
    lines: list[str] = []
    if bound_inputs:
        lines.append("bound inputs:")
        for key, value in bound_inputs.items():
            lines.append(f"· {key}: {_format_input_value(value)}")
    if used_lines:
        if lines:
            lines.append("")
        lines.extend(used_lines)
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


_WORKFLOW_NODE_TYPE = "kolsch/nodes/workflow"
"""feat-104: the sub-workflow node literal (``parser.py:429``).

A ``WorkflowNodeRow`` whose ``_node_type`` matches this string is a
``kolsch/nodes/workflow`` invocation — the parent side of a
parent→child sub-workflow edge. Only these rows grow the forward
:class:`WorkflowCallLink` to the child's peer card (feat-104 gate,
mirroring the ``== "kolsch/nodes/agent"`` gate in ``set_output``).
"""


_CHILD_STATUS_SUFFIXES: dict[str, str] = {
    "succeeded": "✓ succeeded",
    "failed": "✗ failed",
    "waiting_for_input": "⚠ waiting",
}
"""feat-104: child-status → link-suffix mapping.

`child_status` is the child run's terminal-ish status
(``WorkflowNodeCalledWorkflowCompletedPayload.child_status``): one of
``succeeded`` / ``failed`` / ``waiting_for_input`` / ``aborted`` /
``timed_out``. The three status glyphs the PINNED visual spec locks
in map here; any other status (``aborted`` / ``timed_out`` / an
unknown string) falls back to the raw status text so the link never
renders a bare bracket.
"""


class WorkflowCallLink(WorkflowAnchorButton):
    """feat-104: focusable forward link from a parent workflow node.

    Mounted inside a ``kolsch/nodes/workflow`` :class:`WorkflowNodeRow`'s
    Contents by :meth:`WorkflowNodeRow.set_child_workflow`. Subclasses
    :class:`~cli.chat.widgets.assistant_prose.WorkflowAnchorButton`
    verbatim — same ``can_focus``, the ``enter`` binding, ``on_click``,
    and ``_activate`` (which calls the app's
    ``action_scroll_to_workflow_card`` with the child run id). Reuse of
    the feat-018 family keeps the CSS ($tool-fg cyan, bold, ``:focus``
    bold reverse) and the ``[▸ <label>]`` bracket format identical to
    the assistant-prose card anchor.

    The only addition is :meth:`update_status`, which re-renders the
    label with a status suffix (``[▸ <child_ref> · <status>]``) when
    the child run completes.
    """

    def __init__(self, *, child_ref: str, child_run_id: str) -> None:
        # feat-104: while the child is live the suffix reads ``running``;
        # `update_status` swaps it on CALLED_WORKFLOW_COMPLETED.
        self._child_ref = child_ref
        self._status_suffix = "running"
        super().__init__(
            label=self._compose_label(),
            run_id=child_run_id,
        )

    def _compose_label(self) -> str:
        """Return ``<child_ref> · <status>`` (bracket added by the base)."""
        return f"{self._child_ref} · {self._status_suffix}"

    def update_status(self, child_status: str) -> None:
        """feat-104: re-render the label suffix from the child status.

        Maps the child run's terminal-ish status onto the PINNED glyph
        suffix (``✓ succeeded`` / ``✗ failed`` / ``⚠ waiting``); any
        other status renders its raw text so the bracket stays intact.
        """
        self._status_suffix = _CHILD_STATUS_SUFFIXES.get(child_status, child_status)
        self._label = self._compose_label()
        self.update(f"[▸ {self._label}]")


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
    # (``running``) routes to `$tool-fg`; terminal states route via
    # the `.-success` / `.-failed` / `.-paused` / `.-cancelling` /
    # `.-cancelled` classes below. The state glyph already renders
    # inside the CollapsibleTitle; no separate glyph column is added.
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
    WorkflowNodeRow.-success    { color: $tool-fg; border-left: outer $tool-fg; }
    WorkflowNodeRow.-failed     { color: $error;   border-left: outer $error; }
    WorkflowNodeRow.-paused     { color: $warning; border-left: outer $warning; }
    WorkflowNodeRow.-cancelling {
        color: $tool-fg;
        opacity: 0.6;
        border-left: outer $tool-fg;
    }
    WorkflowNodeRow.-cancelled  { color: $hint;    border-left: outer $hint; }
    WorkflowNodeRow.-pending    { color: $hint;    border-left: outer $hint; }
    WorkflowNodeRow.-skipped    { color: $hint;    border-left: outer $hint; }
    """

    # State glyphs live in `src/cli/chat/state_tokens.py` — the
    # single canonical `STATE_TOKENS` table shared with `CardWidget`
    # (qol-075). qol-084 migrated this row's reactive `state` to the
    # canonical vocabulary; `_LEGACY_ALIASES` and the DeprecationWarning
    # scaffolding are gone. Widget internals key on canonical names
    # directly.

    _TERMINAL_CSS_STATES = (
        "success",
        "failed",
        "paused",
        "cancelling",
        "cancelled",
        "pending",
        "skipped",
    )
    """States that map to a `.-<state>` CSS class.

    bug-096: this set is NO LONGER strictly "terminal" — it is the set
    of states `watch_state` iterates to remove/apply the `.-<state>`
    CSS class. It now includes the NON-terminal ``pending`` (the
    pre-mount placeholder rail) alongside ``skipped``. Buffer-freeze
    keys on the SEPARATE ``_TERMINAL_STATES`` frozenset, so adding
    ``pending`` here paints the ``.-pending`` rail WITHOUT freezing the
    mount buffer — the exact separation that makes a non-terminal
    colored rail safe.
    """

    _ERROR_PREVIEW_MAX_LEN = 60
    """Coordinator #4 lock — fixed 60 char error preview truncation."""

    _OUTPUT_PREVIEW_MAX_LEN = _BODY_MAX_CHARS
    """bug-034: pretty-printed `payload.output` truncation cap."""

    state = reactive("running", init=False)
    """Reactive; `watch_state` applies the `.-<state>` CSS class.

    qol-084: values are canonical
    (``running``/``success``/``failed``/``paused``/``cancelling``/
    ``cancelled``). The default is ``running`` — a freshly-mounted
    node is always in flight (a workflow only mounts the row on the
    ``node.started`` event).
    """

    def __init__(
        self,
        *,
        node_name: str,
        trace_id: str | None = None,
        node_type: str | None = None,
        invocation_id: str | None = None,
        initial_state: str = "running",
    ) -> None:
        self._node_name = node_name
        self._trace_id = trace_id
        self._node_type = node_type
        # bug-095: the row's own invocation_id. The renderer keys its
        # ``_node_rows`` map on ``(node_name, invocation_id)`` and passes
        # the payload's top-level ``invocation_id`` through here. Rows did
        # NOT carry this before — ``add_iteration_child`` needs it to
        # compare a stored iteration child against an incoming one (the
        # same-index-different-invocation idempotency guard).
        self._invocation_id: str | None = invocation_id
        # bug-095: map-iteration child machinery. A ``kolsch/nodes/map``
        # row collects one child row per iteration keyed by
        # ``iteration_index`` (0-based); each child is stamped with its
        # index / total / a back-pointer to the parent map row so its
        # ``set_state`` transitions roll up into the parent's title.
        self._iteration_children: dict[int, WorkflowNodeRow] = {}
        self._is_iteration_child: bool = False
        self._iteration_index: int | None = None
        self._iteration_total: int | None = None
        self._parent_map_row: WorkflowNodeRow | None = None
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
        # bug-094: rendered companion for the bash `command` preview. When
        # present AND != the raw template, the input section shows the
        # rendered form as the primary `command:` line and the raw template
        # on a subordinate `template:` line.
        self._pending_command_rendered: str | None = None
        # bug-094: persistent copies of the bash command + its rendered
        # companion, kept AFTER `_flush_pending_previews` resets the pending
        # slots to None (bug-036c invariant: `_pending_command is None`
        # post-mount). These let `_refresh_bash_input_body` re-render the
        # already-mounted body when a later `set_workflow_inputs_used` /
        # `set_bound_inputs` arrives out of order. `None` ⇒ not a bash
        # command section (compute/map/workflow previews leave these unset).
        self._command_text: str | None = None
        self._command_rendered: str | None = None
        # bug-094: stashed non-bash config preview (compute `value`, map
        # `over`, nested-workflow `ref`/`binds`), deferred until mount like
        # `_pending_command`. `("value"|"over", raw, rendered)` or
        # `("workflow", ref, binds, rendered_binds)`.
        self._pending_config_preview: tuple[Any, ...] | None = None
        # bug-094: the subset of workflow-scope inputs this node's templated
        # fields referenced (from `NODE_STARTED.payload.workflow_inputs_used`).
        # Surfaced as a `workflow inputs used:` sub-block on the live bash
        # input body. Stashed BEFORE the section mounts (mirrors
        # `_bound_inputs`).
        self._workflow_inputs_used: dict[str, Any] = {}
        # bug-037: per-type sections. Bash rows own an `input`
        # section (bound_inputs + command), an `output` section
        # (stdout), and an `exit: <code>` section. Agent rows own
        # `input: <key>` sections plus `response`/`response (N)` +
        # per-tool_call_id `tool: <name>` sections.
        self._response_count: int = 0
        self._bound_inputs: dict[str, Any] = {}
        self._bash_input_section: NodeSection | None = None
        self._bash_output_section: Widget | None = None
        self._bash_exit_section: Widget | None = None
        # qol-108: write_file rows own two top-level NodeSections
        # (``▶ path`` + ``▶ content``). Deferred until mount via the
        # same pattern as ``_pending_command``: a stashed 4-tuple
        # ``(path, content, rendered_path, rendered_content)``.
        self._pending_write_file_preview: (
            tuple[str, str, str | None, str | None] | None
        ) = None
        self._write_file_path_section: NodeSection | None = None
        self._write_file_content_section: NodeSection | None = None
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
        # feat-104: forward link to a child sub-workflow's peer card.
        # Only populated on a ``kolsch/nodes/workflow`` row via
        # ``set_child_workflow``. ``_child_ref`` feeds the collapsed-
        # title hint (``▸ <child_ref>``); ``_child_link`` is the mounted
        # focusable widget (idempotent — a replayed CALLED_WORKFLOW does
        # not mount a second link).
        self._child_ref: str | None = None
        self._child_run_id: str | None = None
        self._child_link: WorkflowCallLink | None = None
        # bug-036b: pre-completion title includes ``(short_type)``.
        # The initial glyph is the spinner table's first frame — a
        # static placeholder for the ~80 ms gap between compose and
        # the first spinner tick (installed in :meth:`on_mount`).
        # qol-084 dropped the hard-coded ``⠋`` literal in favor of
        # ``SPINNER_FRAMES[0]`` — same visual, canonical source; the
        # bug-036b snapshot golden pins on the same character either
        # way, so no snapshot regeneration is triggered here.
        # bug-096: the leading glyph is spinner-frame 0 ONLY for the
        # animated ``running`` state (a freshly-mounted in-flight
        # node). A non-animated ``initial_state`` (``pending``) starts
        # with its resting canonical glyph (``○``) so a placeholder
        # never appears to spin before its first frame tick.
        if initial_state in _ANIMATED_STATES:
            leading_glyph = SPINNER_FRAMES[0]
        else:
            try:
                leading_glyph = resolve_state(initial_state).glyph
            except KeyError:
                leading_glyph = "?"
        title = f"{leading_glyph} {node_name}"
        if self._short_type:
            title += f" ({self._short_type})"
        super().__init__(title=title, collapsed=True)
        # bug-096: assign the reactive AFTER ``super().__init__`` so
        # ``watch_state`` fires and applies the ``.-<state>`` class
        # (e.g. ``.-pending``). The spinner-stop branch inside
        # ``watch_state`` is a no-op here (``_spinner_handle`` is
        # ``None`` until ``on_mount``); ``pending`` is not in
        # ``_TERMINAL_STATES`` so no buffer-freeze. The ``running``
        # default already matches the reactive default — skip the
        # assignment so existing rows behave exactly as before.
        if initial_state != "running":
            self.state = initial_state

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
        # bug-095: an iteration child's state change rolls up into the
        # parent map row's title counter (X done, Y running, Z failed).
        self._notify_parent_state_change()

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

    def set_child_workflow(self, child_run_id: str, child_ref: str) -> None:
        """feat-104: mount a forward link to the child sub-workflow's card.

        Called by
        `WorkflowRenderer._on_called_workflow`
        when the parent's ``workflow.node.called_workflow`` event lands.
        Gated on ``self._node_type == "kolsch/nodes/workflow"`` (same gate
        style as the ``kolsch/nodes/agent`` check in ``set_output``); a
        non-workflow row ignores the call.

        Mounts a focusable :class:`WorkflowCallLink` inside the row's
        Contents (revealed on expand — putting it here avoids fighting
        the Collapsible's Enter-toggles-expand behaviour) and adds a
        ``▸ <child_ref>`` discoverability hint to the collapsed title.
        Idempotent: a replayed CALLED_WORKFLOW does NOT mount a second
        link (AC #7).
        """
        if self._node_type != _WORKFLOW_NODE_TYPE:
            return
        if self._child_link is not None:
            # feat-104 AC #7: replay-safe — the link is already mounted.
            return
        self._child_run_id = child_run_id
        self._child_ref = child_ref
        link = WorkflowCallLink(child_ref=child_ref, child_run_id=child_run_id)
        self._child_link = link
        with contextlib.suppress(Exception):
            self._mount_in_contents(link)
        # feat-104: surface the `▸ <child_ref>` hint on the collapsed
        # title so the link is discoverable without expanding the row.
        self._update_title()

    def set_child_status(self, child_status: str) -> None:
        """feat-104: update the child link's status suffix on completion.

        Called by
        `WorkflowRenderer._on_called_workflow_completed`
        when the parent's ``workflow.node.called_workflow_completed``
        event lands. Gated on the workflow node type; a no-op when no
        link was mounted (e.g. a CALLED_WORKFLOW_COMPLETED that arrived
        without a preceding CALLED_WORKFLOW).
        """
        if self._node_type != _WORKFLOW_NODE_TYPE:
            return
        if self._child_link is None:
            return
        self._child_link.update_status(child_status)

    def set_bound_inputs(self, inputs: dict[str, Any]) -> None:
        """bug-037 + qol-108: stash bound inputs; own the bash ``▶ input`` section.

        Ownership shift (qol-108): the bash ``▶ input`` section is now
        owned by :meth:`set_bound_inputs` / :meth:`set_workflow_inputs_used`
        (previously :meth:`set_command_preview` mounted it as a lumped
        section). Called BEFORE the config-preview setters by the renderer
        so bash rows still get their binds surface before the ``▶ command``
        section mounts. Empty-block-suppressed: no mount when both
        ``bound_inputs`` and ``workflow_inputs_used`` are empty (matches
        bug-037's convention).
        """
        self._bound_inputs = dict(inputs)
        self._refresh_bash_binds_body()

    def set_workflow_inputs_used(self, inputs: dict[str, Any]) -> None:
        """bug-094 + qol-108: stash the workflow-scope inputs this node used.

        Mirrors :meth:`set_bound_inputs`. Surfaced as a ``workflow inputs
        used:`` sub-block on the bash ``▶ input`` body (below ``bound
        inputs:``). For compute/map/workflow rows the stash is picked up
        by :meth:`_config_preview_body` — the mount ownership shift
        applies to bash only.
        """
        self._workflow_inputs_used = dict(inputs)
        self._refresh_bash_binds_body()

    def set_command_preview(self, command: str, *, rendered: str | None = None) -> None:
        """qol-108 (was bug-036c + bug-037 + bug-094): mount ``▶ command``.

        Post-qol-108 this method owns the ``▶ command`` section only. The
        bash ``▶ input`` section (binds surface) is owned by
        :meth:`set_bound_inputs` / :meth:`set_workflow_inputs_used`. When
        ``rendered`` differs from ``command``, the section body shows the
        rendered form as primary and adds a ``template:`` sub-block with
        the raw (see :func:`_render_command_block`).
        """
        if self._command_widget is not None:
            # Section already mounted (out-of-order setter): refresh body.
            self._command_text = command
            self._command_rendered = rendered
            self._refresh_command_body()
            return
        self._pending_command = command
        self._pending_command_rendered = rendered
        if self.is_mounted:
            self._flush_pending_previews()

    def set_write_file_preview(
        self,
        path: str,
        content: str,
        *,
        rendered_path: str | None = None,
        rendered_content: str | None = None,
    ) -> None:
        """qol-108: mount ``▶ path`` + ``▶ content`` sections for write_file.

        Mirrors :meth:`set_command_preview`'s defer-until-mount pattern
        (line pattern ``_pending_command`` / ``_flush_pending_previews``):
        when the row isn't yet composed the tuple is stashed and flushed
        from :meth:`on_mount`. Each section is a top-level ``NodeSection``
        with a single ``Static`` body; rendered-vs-template convention is
        preserved (see :func:`_render_path_body` /
        :func:`_render_content_body`).

        Idempotent: a second call with the section already mounted is a
        no-op (write_file previews don't refresh in place today).
        """
        if self._write_file_path_section is not None:
            return
        self._pending_write_file_preview = (
            path,
            content,
            rendered_path,
            rendered_content,
        )
        if self.is_mounted:
            self._flush_pending_previews()

    def set_value_preview(self, value: str, *, rendered: str | None = None) -> None:
        """bug-094: mount a compute ``value`` preview (rendered + template)."""
        self._stash_config_preview(("value", value, rendered))

    def set_over_preview(self, over: str, *, rendered: str | None = None) -> None:
        """bug-094: mount a map ``over`` preview (rendered + template)."""
        self._stash_config_preview(("over", over, rendered))

    def set_workflow_binds_preview(
        self,
        ref: str,
        binds: dict[str, Any],
        rendered_binds: dict[str, Any],
    ) -> None:
        """bug-094: mount a nested-workflow ``ref`` + rendered ``binds`` preview.

        Shows the sub-workflow ``ref:`` line plus a per-bind ``key: rendered
        (template: raw)`` block so the parent row surfaces what each bind
        resolved to at fire-time — the class the design review flagged as
        "where propagation matters most".
        """
        self._stash_config_preview(("workflow", ref, binds, rendered_binds))

    def _stash_config_preview(self, spec: tuple[Any, ...]) -> None:
        """bug-094: stash a non-bash config preview + flush if mounted.

        Mirrors :meth:`set_command_preview`'s defer-until-mount pattern so a
        preview set while the row is still composing still renders. ``spec``
        is ``("value"|"over", raw, rendered)`` or
        ``("workflow", ref, binds, rendered_binds)``.
        """
        if self._command_widget is not None:
            return
        self._pending_config_preview = spec
        if self.is_mounted:
            self._flush_pending_previews()

    def _config_preview_body(self, spec: tuple[Any, ...]) -> str:
        """bug-094: build a non-bash config-preview section body from ``spec``."""
        lines: list[str] = []
        used_lines = _render_workflow_inputs_used_block(self._workflow_inputs_used)
        kind = spec[0]
        if kind == "workflow":
            _, ref, binds, rendered_binds = spec
            lines.append(f"ref: {ref}")
            if binds:
                lines.append("")
                lines.append("binds:")
                for key in binds:
                    rendered = rendered_binds.get(key)
                    raw = binds.get(key)
                    if rendered is not None and str(rendered) != str(raw):
                        lines.append(f"· {key}: {_truncate(str(rendered), 500)}")
                        lines.append(f"  template: {_truncate(str(raw), 500)}")
                    else:
                        lines.append(f"· {key}: {_truncate(str(raw), 500)}")
            if used_lines:
                lines.append("")
                lines.extend(used_lines)
            return "\n".join(lines)
        # compute `value` / map `over`: rendered primary + `template:` sub.
        label, raw, rendered = spec
        if used_lines:
            lines.extend(used_lines)
            lines.append("")
        lines.append(f"{label}:")
        if rendered is not None and rendered != raw:
            lines.append(_truncate(rendered, 500))
            lines.append("template:")
            lines.append(_truncate(raw, 500))
        else:
            lines.append(_truncate(raw, 500))
        return "\n".join(lines)

    def _flush_pending_previews(self) -> None:
        """qol-108 (was bug-036c + bug-037 + bug-094): mount deferred previews.

        Post-qol-108 the bash flush mounts a top-level ``▶ command`` section
        (the ``▶ input`` binds section is owned separately by
        :meth:`set_bound_inputs` / :meth:`set_workflow_inputs_used`).
        Write_file rows mount two top-level sections (``▶ path`` +
        ``▶ content``). Compute/map/workflow rows still flush the stashed
        non-bash preview into an ``▶ input`` section (unchanged shape).
        Guarded against a double-mount via ``_command_widget`` (bash /
        compute / map / workflow) and ``_write_file_path_section``
        (write_file).
        """
        # qol-108: bash binds section may need mounting first (if
        # `set_bound_inputs` fired before `on_mount`).
        self._refresh_bash_binds_body()
        # write_file has its own two-section flush; run independently of the
        # `_command_widget` guard because write_file does not use it.
        if (
            self._pending_write_file_preview is not None
            and self._write_file_path_section is None
        ):
            path, content, rendered_path, rendered_content = (
                self._pending_write_file_preview
            )
            self._pending_write_file_preview = None
            self._mount_write_file_sections(
                path, content, rendered_path, rendered_content
            )
        if self._command_widget is not None:
            return
        if self._pending_command is not None:
            # Promote the pending bash command to the persistent fields so a
            # later out-of-order setter can refresh the body; then clear the
            # pending slots (bug-036c invariant: `_pending_command is None`
            # post-mount).
            self._command_text = self._pending_command
            self._command_rendered = self._pending_command_rendered
            self._pending_command = None
            self._pending_command_rendered = None
            body = "\n".join(
                _render_command_block(self._command_text, self._command_rendered)
            )
            section = self._new_command_section(body)
            self._command_widget = section
            return
        if self._pending_config_preview is not None:
            body = self._config_preview_body(self._pending_config_preview)
            section = self._new_bash_input_section(body)
            self._bash_input_section = section
            self._command_widget = section

    def _refresh_bash_binds_body(self) -> None:
        """qol-108 (was ``_refresh_bash_input_body``): own the bash binds section.

        Bash rows: mount the ``▶ input`` (binds) section on first non-empty
        setter (from ``_flush_pending_previews`` running in ``on_mount``,
        OR from a live setter after mount), refresh in place on subsequent
        setters. Both ``_bound_inputs`` and ``_workflow_inputs_used`` empty
        → no mount (empty-block-suppressed convention per AC #5).

        The mount call is deferred to :meth:`_flush_pending_previews` when
        the row hasn't composed yet — a pre-compose setter can't find
        ``Collapsible.Contents`` and its ``_mount_in_contents`` fallback
        would land the Static as a sibling of Contents (violating
        bug-035). ``_flush_pending_previews`` runs from ``on_mount``, at
        which point Contents is queryable.

        Non-bash rows are left alone: compute/map/workflow rows carry
        ``workflow_inputs_used`` inside their own ``▶ input`` config-preview
        section via :meth:`_config_preview_body` (unchanged shape).
        """
        if self._node_type != "kolsch/nodes/bash":
            return
        body = _render_bash_binds_body(
            self._bound_inputs,
            workflow_inputs_used=self._workflow_inputs_used,
        )
        if self._bash_input_section is None:
            if not body:
                return
            # Pre-compose setter → defer the mount to ``_flush_pending_previews``
            # (invoked by ``on_mount`` once Contents exists). Note the
            # `_bash_input_section` remains None so the flush path retries.
            try:
                self.query_one(Collapsible.Contents)
            except Exception:  # noqa: BLE001 — Contents not composed yet
                return
            self._bash_input_section = self._new_bash_input_section(body)
            return
        inner = self._bash_input_section._contents_list[0]
        if isinstance(inner, Static):
            with contextlib.suppress(Exception):
                inner.update(body)

    def _refresh_command_body(self) -> None:
        """qol-108: re-render the mounted ``▶ command`` body in place.

        No-op when the section isn't mounted yet or when the mounted
        widget doesn't carry the bash command (compute/map/workflow
        previews own the ``_command_widget`` slot with their own body).
        """
        if self._command_widget is None or self._command_text is None:
            return
        body = "\n".join(
            _render_command_block(self._command_text, self._command_rendered)
        )
        # ``_command_widget`` is the ``▶ command`` NodeSection; narrow to
        # its inner Static (mounted by :meth:`_new_command_section`).
        section = self._command_widget
        try:
            inner = section._contents_list[0]  # type: ignore[attr-defined]
        except (AttributeError, IndexError):
            return
        if isinstance(inner, Static):
            with contextlib.suppress(Exception):
                inner.update(body)

    def _new_bash_input_section(self, body_text: str) -> NodeSection:
        """Build + mount the bash ``input`` NodeSection (binds surface)."""
        from cli.chat.widgets.node_section import NodeSection  # noqa: PLC0415

        section = NodeSection("input")
        inner = Static(body_text, classes="node-command-preview", markup=False)
        section._contents_list.append(inner)
        with contextlib.suppress(Exception):
            self._mount_in_contents(section)
        return section

    def _new_command_section(self, body_text: str) -> NodeSection:
        """qol-108: build + mount a top-level ``▶ command`` NodeSection."""
        from cli.chat.widgets.node_section import NodeSection  # noqa: PLC0415

        section = NodeSection("command")
        inner = Static(body_text, classes="node-command-preview", markup=False)
        section._contents_list.append(inner)
        with contextlib.suppress(Exception):
            self._mount_in_contents(section)
        return section

    def _mount_write_file_sections(
        self,
        path: str,
        content: str,
        rendered_path: str | None,
        rendered_content: str | None,
    ) -> None:
        """qol-108: mount ``▶ path`` + ``▶ content`` for a write_file row.

        Ordering pinned by AC #6: ``▶ path`` mounts before ``▶ content``.
        Each section wraps a single ``Static`` with the rendered-vs-
        template convention preserved via :func:`_render_path_body` /
        :func:`_render_content_body`.
        """
        from cli.chat.widgets.node_section import NodeSection  # noqa: PLC0415

        path_body = _render_path_body(path, rendered_path)
        path_section = NodeSection("path")
        path_section._contents_list.append(
            Static(path_body, classes="node-command-preview", markup=False),
        )
        with contextlib.suppress(Exception):
            self._mount_in_contents(path_section)
        self._write_file_path_section = path_section

        content_body = _render_content_body(content, rendered_content)
        content_section = NodeSection("content")
        content_section._contents_list.append(
            Static(content_body, classes="node-command-preview", markup=False),
        )
        with contextlib.suppress(Exception):
            self._mount_in_contents(content_section)
        self._write_file_content_section = content_section

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
            state = "failed" if _is_failed_exit(exit_code) else "success"
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
        # bug-037 + qol-108: bash rows mount the `▶ input` section from
        # `set_bound_inputs` / `set_workflow_inputs_used` when either is
        # non-empty. If it already exists (bash with binds), reuse it.
        # Otherwise (agent nodes, or bash without binds), mount a generic
        # Input from `_bound_inputs`.
        if self._bash_input_section is None:
            input_body = _render_bound_inputs_body(
                self._bound_inputs,
                workflow_inputs_used=self._workflow_inputs_used,
            )
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
    # bug-095: map-iteration child rows
    # ------------------------------------------------------------------

    def add_iteration_child(
        self,
        child_row: WorkflowNodeRow,
        iteration_index: int,
        iteration_total: int,
    ) -> WorkflowNodeRow:
        """bug-095: mount ``child_row`` as this map row's iteration ``[i]``.

        A ``kolsch/nodes/map`` node spawns N iterations of one applied
        node; each iteration is rendered as a CHILD row inside this map
        row's ``Collapsible.Contents``, index-ordered by
        ``iteration_index`` (0-based) regardless of arrival order.

        Idempotent on a repeated ``iteration_index``: returns the stored
        child unchanged. If the incoming child's ``_invocation_id``
        differs from the stored child's, that is an executor bug
        (two distinct invocations claiming the same iteration slot) —
        log a WARNING and keep the original.

        Collapse-by-default at N>10 is a legibility threshold: children
        of a large map start collapsed so the map header stays legible.
        A user who click-expands a specific iteration is not overridden
        (this method never re-collapses an existing child).
        """
        existing = self._iteration_children.get(iteration_index)
        if existing is not None:
            if existing._invocation_id != child_row._invocation_id:
                _LOGGER.warning(
                    "bug-095: iteration_index=%s on map node %s already "
                    "mounted with invocation_id=%s; ignoring a second "
                    "invocation_id=%s (executor bug — duplicate iteration "
                    "slot).",
                    iteration_index,
                    self._node_name,
                    existing._invocation_id,
                    child_row._invocation_id,
                )
            return existing
        # Stamp the child so its title reads ``<name>[<index>]`` and its
        # state transitions roll up into this map row's title.
        child_row._iteration_index = iteration_index
        child_row._iteration_total = iteration_total
        child_row._parent_map_row = self
        child_row._is_iteration_child = True
        child_row._update_title()
        # Collapse-by-default only for large maps (>10). Small maps show
        # their iterations expanded. WorkflowNodeRow constructs collapsed,
        # so a small map must explicitly expand.
        child_row.collapsed = iteration_total > 10
        # Insert index-ordered: mount BEFORE the first already-mounted
        # child whose index is greater than this one; append when none.
        before_widget: WorkflowNodeRow | None = None
        for key in sorted(self._iteration_children.keys()):
            if key > iteration_index:
                before_widget = self._iteration_children[key]
                break
        self._iteration_children[iteration_index] = child_row
        with contextlib.suppress(Exception):
            self._mount_in_contents(child_row, before=before_widget)
        # Refresh this map row's title so the rollup counter appears as
        # soon as the first child mounts.
        self._update_title()
        return child_row

    def _notify_parent_state_change(self) -> None:
        """bug-095: tell the parent map row one of its children changed.

        Called at the end of :meth:`set_state`. A no-op for non-iteration
        rows (``_parent_map_row is None``).
        """
        if self._parent_map_row is not None:
            self._parent_map_row._on_child_state_changed()

    def _on_child_state_changed(self) -> None:
        """bug-095: refresh the map row's rollup title after a child change."""
        self._update_title()

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
                    # qol-109: cap per-key content dict values at the row
                    # sink so the resulting `UserChatbox` subtree stays
                    # cheap to first-arrange when its NodeSection expands.
                    capped_value = _cap_with_tail(str(value), max_chars=16_000)
                    section._contents_list.append(UserChatbox(capped_value, ts))
                    self._mount_in_contents(section)
            else:
                section = NodeSection("input")
                # qol-109: cap flat-branch text at the row sink; see the
                # per-key cap above for rationale.
                capped_text = _cap_with_tail(text, max_chars=16_000)
                section._contents_list.append(UserChatbox(capped_text, ts))
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
            # qol-109: cap the assistant response text at the row sink.
            # Textual's `Markdown` mounts one `MarkdownBlock` per token
            # and measures each on first display-flip; large responses
            # cost ~1.8 s per toggle uncapped. See spec qol-109.
            capped_text = _cap_with_tail(text, max_chars=16_000)
            section._contents_list.append(AssistantProse(capped_text))
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
                new_state = "failed" if failed else "success"
                if hasattr(existing, "set_state"):
                    existing.set_state(new_state)
                block = self._tool_blocks.get(tool_call_id)
                if block is not None:
                    # qol-109: cap the transition body at the row sink
                    # before it lands in `ToolCallBlock.update_body`.
                    # Large `browse_url` / `read_file` / `bash` outputs
                    # otherwise pay the same measurement cost as agent
                    # responses when the `▶ tool: <name>` section opens.
                    capped_body = _cap_with_tail(body, max_chars=16_000)
                    self._populate_tool_call_result(block, capped_body, failed=failed)
                return
            state = (
                "running"
                if not body and not failed
                else ("failed" if failed else "success")
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
                except Exception:  # noqa: BLE001 — best-effort UI mount; logged, must not kill the row
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

            # qol-109: cap notice text at the row sink for symmetry with
            # the other three mount methods (assistant / tool / user).
            # `NoticeWidget` renders a single Static so the reflow cost
            # is smaller than `AssistantProse`, but the cap keeps every
            # row-sink surface uniform and future-proofs a very large
            # template-warning payload.
            capped_text = _cap_with_tail(text, max_chars=16_000)
            widget = NoticeWidget(text=capped_text, kind=level)
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
            except Exception:  # noqa: BLE001 — best-effort UI mount; logged, must not kill the row
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

    def watch_collapsed(self, old: bool, new: bool) -> None:  # noqa: FBT001 — Textual reactive-watcher signature
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
                loop.create_task(self._drain_pending_mounts())  # noqa: RUF006 — fire-and-forget teardown-path fallback; no lifecycle owner

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
            # State-tokens: the reactive always holds a canonical name
            # (qol-084); unknown names would still raise ``KeyError``
            # from ``resolve_state``, so mirror the pre-qol-075
            # ``.get(..., "?")`` fallback and paint ``?`` on a stray
            # state rather than crashing the title paint.
            try:
                glyph = resolve_state(self.state).glyph
            except KeyError:
                glyph = "?"
        # spec-078: drop the leading glyph when it would double with
        # Collapsible's own `▶`/`▼` chevron (canonical `running` glyph
        # is `▸` — same visual family). Terminal glyphs (`✓`/`✗`/`⚠`/
        # `─`) are cross/warning/rule characters — different family,
        # no collision, keep them. Spinner frames (`⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`) are
        # braille dots — no collision, keep.
        # bug-095: an iteration child's name carries the iteration index
        # as a bracket suffix (``score_one_resume[3]``). Non-iteration
        # rows (``_iteration_index is None``) are unchanged.
        node_label = self._node_name
        if self._iteration_index is not None:
            node_label = f"{self._node_name}[{self._iteration_index}]"
        base = node_label if glyph == "▸" else f"{glyph} {node_label}"
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
        # feat-104: `▸ <child_ref>` discoverability hint for the forward
        # link to the child sub-workflow's card. Recomputed here (not
        # appended once) because the ~80 ms spinner tick re-renders the
        # whole title — a one-shot append would be clobbered on the next
        # frame. Only ``kolsch/nodes/workflow`` rows ever set
        # ``_child_ref`` (the gate lives in ``set_child_workflow``).
        if self._child_ref is not None:
            base += f"   ▸ {self._child_ref}"
        # bug-095: a map row with iteration children carries a live
        # rollup counter — `(map · N iterations · X done, Y running,
        # Z failed)`. Recomputed here (like the sub-agent counter) so
        # the ~80 ms spinner tick can't clobber a one-shot append.
        if self._iteration_children:
            base += f"   {self._iteration_rollup()}"
        self.title = base

    def _iteration_rollup(self) -> str:
        """bug-095: `(map · N iterations · X done, Y running, Z failed)`.

        ``done`` = ``success``; ``failed`` = ``failed``/``cancelled``;
        ``running`` = everything else (including ``paused`` — iterations
        never independently pause today, but handle it defensively as a
        still-in-flight state).
        """
        children = self._iteration_children.values()
        total = len(children)
        done = sum(1 for c in children if c.state == "success")
        failed = sum(1 for c in children if c.state in ("failed", "cancelled"))
        running = total - done - failed
        return (
            f"(map · {total} iterations · "
            f"{done} done, {running} running, {failed} failed)"
        )

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

    def _mount_in_contents(
        self,
        widget: Widget,
        *,
        before: Widget | None = None,
    ) -> None:
        """bug-035: mount ``widget`` inside the Collapsible's Contents slot.

        Textual's :class:`Collapsible.compose_add_child` only wires
        the *context-manager* compose syntax to the internal
        ``Contents`` container — a bare ``self.mount(child)`` on an
        already-composed Collapsible mounts as a **sibling** of the
        ``CollapsibleTitle`` and ``Contents``. Only ``Contents``
        responds to the ``.-collapsed > Contents { display: none }``
        CSS, so sibling mounts stay visible regardless of the row's
        collapsed state.

        bug-095: ``before`` forwards to Textual's
        ``Widget.mount(widget, before=other)`` so map-iteration children
        can be inserted index-ordered rather than always appended. A
        ``before`` widget that isn't a live sibling of ``Contents`` (the
        row hasn't composed yet) falls through to a plain append.
        """
        try:
            contents = self.query_one(Collapsible.Contents)
        except Exception:  # noqa: BLE001 — Contents not composed yet
            contents = None
        if contents is not None:
            if before is not None:
                with contextlib.suppress(Exception):
                    contents.mount(widget, before=before)
                    return
            contents.mount(widget)
            return
        # Row not fully mounted yet — mount at the outer level.
        self.mount(widget)


__all__ = ["WorkflowCallLink", "WorkflowNodeRow"]
