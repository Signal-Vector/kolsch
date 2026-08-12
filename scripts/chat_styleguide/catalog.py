"""Widget catalog for the chat styleguide capture harness.

This module defines the complete inventory of chat widgets, their
meaningful visual states, and the driver callables that transition
each widget into those states via production paths (not naive
attribute pokes).

The catalog is consumed by the capture engine (``capture.py``) to
render each widget x state x variant (color + mono) into SVG tiles
for the gallery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from workflows.ids import WorkflowRunId

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

from cli.chat.widgets.approval_args_row import ApprovalArgsRow
from cli.chat.widgets.approval_block import ApprovalBlock
from cli.chat.widgets.approval_choice_pane import ApprovalChoicePane
from cli.chat.widgets.approval_from_edit import ApprovalFromEdit
from cli.chat.widgets.approval_pattern_edit import ApprovalPatternEdit
from cli.chat.widgets.approval_save_confirm import ApprovalSaveConfirm
from cli.chat.widgets.assistant_prose import AssistantProse
from cli.chat.widgets.card_body.confirm import ConfirmRenderer
from cli.chat.widgets.card_body.markdown import MarkdownRenderer
from cli.chat.widgets.card_body.progress import ProgressRenderer
from cli.chat.widgets.card_body.workflow import WorkflowRenderer
from cli.chat.widgets.card_prompt_pane import CardPromptPane
from cli.chat.widgets.card_widget import CardWidget
from cli.chat.widgets.compaction_block import CompactionBlock
from cli.chat.widgets.composer import Composer
from cli.chat.widgets.force_kill_confirm import ForceKillConfirm
from cli.chat.widgets.info_bar import InfoBar
from cli.chat.widgets.node_section import NodeSection
from cli.chat.widgets.notice import NoticeWidget
from cli.chat.widgets.notice_slot import NoticeSlot
from cli.chat.widgets.scrollback_cue import ScrollbackCue
from cli.chat.widgets.slash_popup import SlashPopup
from cli.chat.widgets.status_bar import StatusBar
from cli.chat.widgets.tool_call_block import ToolCallBlock
from cli.chat.widgets.user_chatbox import UserChatbox
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow


@dataclass(frozen=True)
class CatalogEntry:
    """One widget x its meaningful visual states.

    Attributes:
        name: Widget class name (e.g. ``"WorkflowNodeRow"``).
        family: Grouping — ``"transcript"``, ``"card_body"``, or ``"chrome"``.
        source_file: Relative path from ``widgets/`` (e.g. ``"card_widget.py"``).
        make: Factory returning a widget instance with mock data.
        states: State name -> driver callable that transitions the widget.
            Drivers may be sync or async; the capture engine handles both.
        widths: Capture widths in columns. Chrome widgets add ``(40,)`` for
            truncation testing; transcript widgets use ``(80,)``.
        expand_collapsible: If True, set ``collapsed=False`` before capture.
        skip: State name -> reason for skipping (unreachable without live engine).
    """

    name: str
    family: str
    source_file: str
    make: Callable[[], Any]
    states: dict[str, Callable[[Any], Awaitable[None] | None]]
    widths: tuple[int, ...] = (80,)
    expand_collapsible: bool = False
    skip: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Stub helpers for widgets that need minimal dependencies
# ---------------------------------------------------------------------------


class _StubWorkflows:
    """Minimal stub for ForceKillConfirm's workflows_impl parameter."""

    async def cancel_run(self, _run_id: str) -> None:
        return None


class _StubStore:
    """Minimal stub for ForceKillConfirm's session_store parameter."""

    async def read_path_to_leaf(self, _thread_id: str) -> object:
        return None


# ---------------------------------------------------------------------------
# Async state drivers for card_body widgets
# ---------------------------------------------------------------------------


def _drive_markdown_content(w: Any) -> None:  # noqa: ANN401
    w.apply_snapshot(
        "## Results\n\nThe task completed **successfully**.\n\n```python\nprint('hello')\n```"
    )


def _drive_progress_determinate(w: Any) -> None:  # noqa: ANN401
    # ProgressRenderer.apply_event reads the payload under the "payload" key.
    w.apply_event(
        {
            "kind": "kolsch.card.set_progress",
            "payload": {"current": 7, "total": 10, "message": "Processing files..."},
        }
    )


def _drive_progress_indeterminate(w: Any) -> None:  # noqa: ANN401
    w.apply_event(
        {
            "kind": "kolsch.card.set_progress",
            "payload": {
                "current": None,
                "total": None,
                "message": "Waiting for response...",
            },
        }
    )


def _drive_confirm_content(w: Any) -> None:  # noqa: ANN401
    w.apply_snapshot("Are you sure you want to delete all temporary files?")


def _wf_event(inner_kind: str, **fields: Any) -> dict[str, Any]:  # noqa: ANN401
    """Wrap a workflow domain event in the ``kolsch.workflow.progress`` envelope."""
    return {
        "kind": "kolsch.workflow.progress",
        "payload": {"kind": inner_kind, **fields},
    }


def _drive_workflow_content(w: Any) -> None:  # noqa: ANN401
    # Feed a short run: start the run, then three nodes (two completed, one
    # still running) so the tile shows a header, node rows, and live counts.
    # No ``ts`` — elapsed is computed against wall-clock, so a fixed past
    # timestamp would render a nonsense duration in a static capture.
    w.apply_event(
        _wf_event(
            "workflow.run.started",
            run_id="run_1",
            workflow_ref="kolsch/workflows/deploy",
            payload={"inputs": {"target": "prod"}},
        )
    )
    for name in ("build", "test", "deploy"):
        w.apply_event(
            _wf_event(
                "workflow.node.started",
                node_name=name,
                trace_id=f"tr_{name}",
                invocation_id=f"inv_{name}",
                payload={"type": "kolsch/nodes/agent"},
            )
        )
    for name in ("build", "test"):
        w.apply_event(
            _wf_event(
                "workflow.node.completed",
                node_name=name,
                trace_id=f"tr_{name}",
                invocation_id=f"inv_{name}",
                payload={"summary": "ok"},
            )
        )


def _drive_workflow_call_link(w: Any) -> None:  # noqa: ANN401
    # feat-104: a `kolsch/nodes/workflow` row that has called a child
    # sub-workflow. `set_child_workflow` mounts the focusable
    # `WorkflowCallLink` ([▸ <child_ref> · <status>]) in the row's
    # Contents + a `▸ <child_ref>` collapsed-title hint;
    # `set_child_status` then updates the suffix to `✓ succeeded`. The
    # row is already mounted when this driver runs, so the
    # `_mount_in_contents` path resolves the Contents slot.
    w.set_state("success")
    w.set_child_workflow("wrk_child_deploy", "kolsch/workflows/deploy-child")
    w.set_child_status("succeeded")


def _drive_workflow_map_iterations(w: Any) -> None:  # noqa: ANN401
    # bug-095: a `kolsch/nodes/map` row with collapsed iteration children.
    # Each iteration is a child `WorkflowNodeRow` mounted inside the map
    # row's Contents via `add_iteration_child`, titled `<name>[<i>]`. With
    # a total of 12 (>10) the children collapse by default; the map row's
    # title carries the live rollup `(map · N iterations · X done, Y
    # running, Z failed)`. Drive a mix of states so the rollup is non-trivial.
    w.set_state("running")
    total = 12
    states = {0: "success", 1: "success", 2: "failed"}
    for i in range(total):
        child = WorkflowNodeRow(
            node_name="score_one_resume",
            trace_id="tr_map",
            node_type="kolsch/nodes/agent",
            invocation_id=f"inv_child_{i}",
        )
        w.add_iteration_child(child, i, total)
        target = states.get(i)
        if target is not None:
            child.set_state(target)


def _expand_approval_args_row(w: Any) -> None:  # noqa: ANN401
    w._expanded = True
    w._refresh_static()


def _set_composer_awaiting(w: Any) -> None:  # noqa: ANN401
    w.awaiting_approval = True


def _set_slash_popup_filtered(w: Any) -> None:  # noqa: ANN401
    # "/s" matches a subset of COMMAND_REGISTRY (/session, /skill) so the
    # filtered state actually shows a narrowed command list.
    w.prefix = "/s"


def _set_slash_popup_empty(w: Any) -> None:  # noqa: ANN401
    w.prefix = "/zzz"


def _set_info_bar_populated(w: Any) -> None:  # noqa: ANN401
    w.agent = "kolsch/agents/main"
    w.model = "claude-sonnet-4"
    w.session = "ses_abc123"
    w.branch = "main"
    w.cwd = "/home/user/project"


def _add_notice_slot_notices(w: Any) -> None:  # noqa: ANN401
    w.add_notice("First notice", "info")
    w.add_notice("Warning: deprecated API", "warning")


def _switch_notice_kind(kind: str) -> Callable[[Any], None]:
    """Return a driver that switches a ``NoticeWidget`` into ``kind``.

    Removes any prior severity class, adds the new one, resets ``_kind``,
    and calls ``update_text`` so the widget re-renders with the correct
    leading glyph.
    """

    def _driver(w: Any) -> None:  # noqa: ANN401
        for cls in ("-warning", "-error"):
            w.remove_class(cls)
        if kind == "warning":
            w.add_class("-warning")
        elif kind == "error":
            w.add_class("-error")
        w._kind = kind
        w.update_text(w._body)

    return _driver


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------


CATALOG: list[CatalogEntry] = [
    # ── Transcript content widgets ────────────────────────────────────
    CatalogEntry(
        name="WorkflowNodeRow",
        family="transcript",
        source_file="workflow_node_row.py",
        make=lambda: WorkflowNodeRow(
            node_name="deduper", trace_id="tr1", node_type="agent"
        ),
        states={
            "starting": lambda w: w.set_state("starting"),
            "running": lambda w: w.set_state("running"),
            "completed": lambda w: w.set_state("completed"),
            "failed": lambda w: w.set_state("failed"),
            "paused": lambda w: w.set_state("paused"),
            "cancelling": lambda w: w.set_state("cancelling"),
            "pruned": lambda w: w.set_state("pruned"),
            # bug-096: pre-mount placeholder (`○`) + branch-skip (`⊘`).
            "pending": lambda w: w.set_state("pending"),
            "skipped": lambda w: w.set_state("skipped"),
        },
        expand_collapsible=True,
    ),
    CatalogEntry(
        # feat-104: a `kolsch/nodes/workflow` node row carrying the
        # focusable forward link to its child sub-workflow's peer card.
        # Distinct catalog name (not "WorkflowNodeRow") so it captures
        # the `WorkflowCallLink` specimen without disturbing the
        # node-row state-set validation.
        name="WorkflowCallLink",
        family="transcript",
        source_file="workflow_node_row.py",
        make=lambda: WorkflowNodeRow(
            node_name="call_child",
            trace_id="tr_wf",
            node_type="kolsch/nodes/workflow",
        ),
        states={
            "workflow_call": _drive_workflow_call_link,
        },
        expand_collapsible=True,
    ),
    CatalogEntry(
        # bug-095: a `kolsch/nodes/map` node row with its per-iteration
        # child rows collapsed underneath. Captures the `<name>[<i>]`
        # child-title suffix + the `(map · N iterations · X done, Y
        # running, Z failed)` rollup on the map header. Distinct catalog
        # name so it does not disturb the WorkflowNodeRow state-set
        # validation.
        name="WorkflowMapIterations",
        family="transcript",
        source_file="workflow_node_row.py",
        make=lambda: WorkflowNodeRow(
            node_name="score_resumes",
            trace_id="tr_map",
            node_type="kolsch/nodes/map",
            invocation_id="inv_map",
        ),
        states={
            "map_iterations": _drive_workflow_map_iterations,
        },
        expand_collapsible=True,
    ),
    CatalogEntry(
        name="CardWidget",
        family="transcript",
        source_file="card_widget.py",
        make=lambda: CardWidget(card_id="c1", kind="markdown", title="Test Card"),
        states={
            "initializing": lambda _w: None,  # default state
            "queued": lambda w: w.apply_status("queued"),
            "paused": lambda w: w.apply_status("paused"),
            "cancelling": lambda w: w.apply_status("cancelling"),
            "success": lambda w: w.apply_close("success", error_message=None),
            "error": lambda w: w.apply_close("error", error_message="test error"),
            "cancelled": lambda w: w.apply_close("cancelled", error_message=None),
        },
        expand_collapsible=False,  # CardWidget default collapsed=False
    ),
    CatalogEntry(
        name="CompactionBlock",
        family="transcript",
        source_file="compaction_block.py",
        make=lambda: CompactionBlock(state="running", title_text="Compacting…"),
        states={
            "running": lambda _w: None,  # default state
            "success": lambda w: w.transition_to_success(
                compacted_count=5, tokens_before=1000, tokens_after=200, trigger="auto"
            ),
            "preview": lambda w: w.transition_to_preview(
                compacted_count=5, tokens_before=1000, tokens_after=200
            ),
            "failure": lambda w: w.transition_to_failure(
                message="timeout", fallback_tier="tier2"
            ),
            "skipped": lambda w: w.transition_to_skipped(reason="below threshold"),
            "revoked": lambda w: w.transition_to_revoked(target_short_id="abc123"),
        },
        expand_collapsible=True,
    ),
    CatalogEntry(
        name="ApprovalBlock",
        family="transcript",
        source_file="approval_block.py",
        make=lambda: ApprovalBlock(
            rule_id="r1", action="tool_use", target="bash", terminal_state=None
        ),
        states={
            "step_0_choose": lambda _w: None,  # default mount
        },
    ),
    CatalogEntry(
        name="ApprovalBlock",
        family="transcript",
        source_file="approval_block.py",
        # Terminal (historical) rows are a construction-time variant via the
        # terminal_state=(allowed, footer) kwarg, so each is its own entry.
        make=lambda: ApprovalBlock(
            rule_id="r1",
            action="tool_use",
            target="bash",
            terminal_state=(True, "allowed once"),
        ),
        states={
            "terminal_allowed": lambda _w: None,  # set at construction
        },
    ),
    CatalogEntry(
        name="ApprovalBlock",
        family="transcript",
        source_file="approval_block.py",
        make=lambda: ApprovalBlock(
            rule_id="r1",
            action="tool_use",
            target="bash",
            terminal_state=(False, "denied once"),
        ),
        states={
            "terminal_denied": lambda _w: None,  # set at construction
        },
    ),
    CatalogEntry(
        name="ApprovalArgsRow",
        family="transcript",
        source_file="approval_args_row.py",
        make=lambda: ApprovalArgsRow(
            target="bash(ls -la /very/long/path/that/might/overflow/terminal/width)"
        ),
        states={
            "collapsed": lambda _w: None,  # default
            "expanded": _expand_approval_args_row,
            "overflow": lambda _w: None,  # long target in constructor
        },
    ),
    CatalogEntry(
        name="AssistantProse",
        family="transcript",
        source_file="assistant_prose.py",
        make=lambda: AssistantProse(
            "**Hello**, here is some markdown with `code`.\n\n- item 1\n- item 2",
            agent_name="Claude",
            timestamp=datetime(2026, 7, 14, 10, 32, 0, tzinfo=UTC),
        ),
        states={},  # frozen widget, single render
    ),
    CatalogEntry(
        name="UserChatbox",
        family="transcript",
        source_file="user_chatbox.py",
        make=lambda: UserChatbox(
            "What's the status of the deployment?",
            datetime(2026, 7, 14, 10, 30, 0, tzinfo=UTC),
        ),
        states={},  # frozen widget
    ),
    CatalogEntry(
        name="ToolCallBlock",
        family="transcript",
        source_file="tool_call_block.py",
        make=lambda: ToolCallBlock(
            tool_name="bash", args="ls -la", body="total 48\ndrwxr-xr-x  5 user staff"
        ),
        states={
            "ok": lambda _w: None,  # default
        },
        expand_collapsible=True,
    ),
    CatalogEntry(
        name="ToolCallBlock",
        family="transcript",
        source_file="tool_call_block.py",
        # `failed` is a construction-time flag, so it is its own entry.
        make=lambda: ToolCallBlock(
            tool_name="bash",
            args="rm -rf /",
            body="Permission denied",
            failed=True,
            recovery_hint="Try with sudo",
        ),
        states={
            "failed": lambda _w: None,  # set at construction
        },
        expand_collapsible=True,
    ),
    CatalogEntry(
        name="NoticeWidget",
        family="transcript",
        source_file="notice.py",
        make=lambda: NoticeWidget(text="Operation completed", kind="info"),
        states={
            "info": _switch_notice_kind("info"),
            "warning": _switch_notice_kind("warning"),
            "error": _switch_notice_kind("error"),
        },
    ),
    CatalogEntry(
        name="ScrollbackCue",
        family="transcript",
        source_file="scrollback_cue.py",
        make=ScrollbackCue,
        states={},  # frozen widget
    ),
    # ── Approval pane widgets ────────────────────────────────────────
    CatalogEntry(
        name="ApprovalChoicePane",
        family="transcript",
        source_file="approval_choice_pane.py",
        make=ApprovalChoicePane,
        states={},  # static 4-choice grid
    ),
    CatalogEntry(
        name="ApprovalPatternEdit",
        family="transcript",
        source_file="approval_pattern_edit.py",
        make=lambda: ApprovalPatternEdit(suggested_pattern="tool:bash:*"),
        states={},
    ),
    CatalogEntry(
        name="ApprovalFromEdit",
        family="transcript",
        source_file="approval_from_edit.py",
        make=lambda: ApprovalFromEdit(suggested_from="user"),
        states={},
    ),
    CatalogEntry(
        name="ApprovalSaveConfirm",
        family="transcript",
        source_file="approval_save_confirm.py",
        make=lambda: ApprovalSaveConfirm(
            "Allow tool:bash:* for all sessions",
            deny=False,
            fallback_label="allow_once",
        ),
        states={},
    ),
    # ── Card body renderers ──────────────────────────────────────────
    CatalogEntry(
        name="MarkdownRenderer",
        family="card_body",
        source_file="card_body/markdown.py",
        make=MarkdownRenderer,
        states={
            "content": _drive_markdown_content,
        },
    ),
    CatalogEntry(
        name="ProgressRenderer",
        family="card_body",
        source_file="card_body/progress.py",
        make=ProgressRenderer,
        states={
            "determinate": _drive_progress_determinate,
            "indeterminate": _drive_progress_indeterminate,
        },
    ),
    CatalogEntry(
        name="ConfirmRenderer",
        family="card_body",
        source_file="card_body/confirm.py",
        make=ConfirmRenderer,
        states={
            "content": _drive_confirm_content,
        },
    ),
    CatalogEntry(
        name="WorkflowRenderer",
        family="card_body",
        source_file="card_body/workflow.py",
        make=lambda: WorkflowRenderer(parent_ref="kolsch/agents/main"),
        states={
            "content": _drive_workflow_content,
        },
    ),
    # ── Chrome widgets ───────────────────────────────────────────────
    CatalogEntry(
        name="Composer",
        family="chrome",
        source_file="composer.py",
        make=Composer,
        states={
            "idle": lambda _w: None,  # default
            "awaiting_approval": _set_composer_awaiting,
        },
        widths=(80, 40),
    ),
    CatalogEntry(
        name="SlashPopup",
        family="chrome",
        source_file="slash_popup.py",
        make=SlashPopup,
        states={
            "filtered": _set_slash_popup_filtered,
            "empty": _set_slash_popup_empty,
        },
        widths=(80, 40),
    ),
    CatalogEntry(
        name="StatusBar",
        family="chrome",
        source_file="status_bar.py",
        make=StatusBar,
        states={
            "idle": lambda _w: None,  # default empty
            "workflow_strip": lambda w: w.set_workflow_strip(
                "3 nodes · 1 running · 2 pending"
            ),
            "toast": lambda w: w.set_toast("Press Esc-Esc to exit"),
        },
        widths=(80, 40),
    ),
    CatalogEntry(
        name="InfoBar",
        family="chrome",
        source_file="info_bar.py",
        make=InfoBar,
        states={
            "populated": _set_info_bar_populated,
        },
        widths=(80, 40),
    ),
    CatalogEntry(
        name="ForceKillConfirm",
        family="chrome",
        source_file="force_kill_confirm.py",
        make=lambda: ForceKillConfirm(
            run_id=WorkflowRunId(f"wrk_{uuid4()}"),
            n_related=2,
            workflows_impl=_StubWorkflows(),
            session_store=_StubStore(),
            thread_id="th_1",
            session_id="ses_1",
        ),
        states={},  # ephemeral modal, single state
    ),
    CatalogEntry(
        name="NodeSection",
        family="transcript",
        source_file="node_section.py",
        make=lambda: NodeSection("Input validation", state="completed"),
        states={
            "starting": lambda w: w.set_state("starting"),
            "running": lambda w: w.set_state("running"),
            "completed": lambda w: w.set_state("completed"),
            "failed": lambda w: w.set_state("failed"),
        },
        expand_collapsible=True,
    ),
    CatalogEntry(
        name="CardPromptPane",
        family="transcript",
        source_file="card_prompt_pane.py",
        make=lambda: CardPromptPane(
            question="Which deployment target?",
            options=["staging", "production", "canary"],
        ),
        states={},  # static skeleton
    ),
    CatalogEntry(
        name="NoticeSlot",
        family="transcript",
        source_file="notice_slot.py",
        make=NoticeSlot,
        states={
            "with_notices": _add_notice_slot_notices,
        },
    ),
]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_catalog(catalog: list[CatalogEntry]) -> None:
    """Assert that each widget's states U skip covers its expected state-set.

    For widgets with known state maps (WorkflowNodeRow, CardWidget,
    CompactionBlock, NodeSection), verify that the catalog entry's
    ``states`` keys plus ``skip`` keys equal the expected set derived
    from the widget's own glyph/state constants.

    Prints warnings for widgets with no states (frozen widgets).
    """
    # Expected state-sets derived from widget source
    expected: dict[str, frozenset[str]] = {
        "WorkflowNodeRow": frozenset(
            {
                "starting",
                "running",
                "completed",
                "failed",
                "paused",
                "cancelling",
                "pruned",
                # bug-096: pre-mount placeholder + branch-skip states.
                "pending",
                "skipped",
            }
        ),
        "CardWidget": frozenset(
            {
                "initializing",
                "queued",
                "paused",
                "cancelling",
                "success",
                "error",
                "cancelled",
            }
        ),
        "CompactionBlock": frozenset(
            {"running", "success", "preview", "failure", "skipped", "revoked"}
        ),
        "NodeSection": frozenset({"starting", "running", "completed", "failed"}),
    }

    for entry in catalog:
        if entry.name in expected:
            exp = expected[entry.name]
            got = frozenset(entry.states.keys()) | frozenset(entry.skip.keys())
            missing = exp - got
            extra = got - exp
            if missing:
                raise ValueError(f"{entry.name}: missing states: {sorted(missing)}")
            if extra:
                print(
                    f"⚠ {entry.name}: extra states not in expected set: {sorted(extra)}"
                )
        elif not entry.states and not entry.skip:
            print(f"i {entry.name}: no states (frozen widget)")


__all__ = ["CATALOG", "CatalogEntry", "validate_catalog"]
