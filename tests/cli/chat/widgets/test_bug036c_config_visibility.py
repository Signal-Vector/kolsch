"""bug-036c — Pilot tests for header ref/inputs + bash command preview.

Six sub-tests covering AC #1 (header seams) and AC #4 (bash command
preview + mount-race protection). The header tests drive
``WorkflowRenderer._on_run_started`` directly; the command tests
construct ``WorkflowNodeRow`` and exercise the pending-slot pattern
that shields against NODE_STARTED / first-sub-agent-event arriving
in the same event-loop drain.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Static

from cli.chat.widgets.card_body.workflow import WorkflowRenderer
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow


class _Host(App[None]):
    """Minimal app that hosts a single mount slot for the tests."""

    CSS = """
    Screen { height: 100vh; }
    #slot { height: 1fr; width: 100%; }
    """

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="slot")


@pytest.mark.asyncio
async def test_header_shows_ref_and_inputs_top_level() -> None:
    """AC #1 top-level: three-line header — ref, inputs, progress."""
    app = _Host()
    async with app.run_test() as pilot:
        renderer = WorkflowRenderer(id="wf")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(renderer)
        await pilot.pause()

        renderer._on_run_started({
            "run_id": "run_abc",
            "workflow_ref": "andre/workflows/jd_review",
            "inputs": {"url": "https://example.com/jd"},
            "ts": "2026-07-09T00:00:00Z",
        })
        text = renderer._header._compute_text()
        lines = text.split("\n")
        assert len(lines) == 3, f"expected 3 lines, got {lines!r}"
        assert lines[0] == "andre/workflows/jd_review"
        assert lines[1] == "inputs: url=https://example.com/jd"
        # qol-077: header counts are bare N/M (no brackets).
        assert lines[2].startswith("0/0")


@pytest.mark.asyncio
async def test_header_flattens_parent_ref_when_nested() -> None:
    """AC #1 nested: parent + ref on ONE flattened first line."""
    app = _Host()
    async with app.run_test() as pilot:
        renderer = WorkflowRenderer(id="wf", parent_ref="parent/wf/outer")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(renderer)
        await pilot.pause()

        renderer._on_run_started({
            "run_id": "run_xyz",
            "workflow_ref": "child/wf/inner",
            "inputs": {"url": "https://x.com"},
            "ts": "2026-07-09T00:00:00Z",
        })
        text = renderer._header._compute_text()
        lines = text.split("\n")
        assert len(lines) == 3, f"expected 3 lines, got {lines!r}"
        assert lines[0] == "↳ from parent/wf/outer · child/wf/inner"
        assert lines[1] == "inputs: url=https://x.com"


@pytest.mark.asyncio
async def test_command_widget_mounts_inside_contents() -> None:
    """AC #4: the command preview lands inside ``Collapsible.Contents``."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(
            node_name="fetch_jd",
            node_type="kolsch/nodes/bash",
        )
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_command_preview("curl https://x.com")
        await pilot.pause()

        contents = row.query_one(Collapsible.Contents)
        child_previews = [
            c for c in contents.walk_children()
            if isinstance(c, Static) and c.has_class("node-command-preview")
        ]
        assert len(child_previews) == 1, (
            f"expected one command preview inside Contents, "
            f"got {len(child_previews)}"
        )


@pytest.mark.asyncio
async def test_command_widget_has_css_class() -> None:
    """AC #4 + bug-037: command Static (inside the input NodeSection) has class."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(
            node_name="fetch_jd",
            node_type="kolsch/nodes/bash",
        )
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_command_preview("curl https://x.com")
        await pilot.pause()

        assert row._command_widget is not None
        # bug-037: the command Static now lives inside the `input`
        # NodeSection; walk into Contents to find it.
        contents = row.query_one(Collapsible.Contents)
        previews = [
            c for c in contents.walk_children()
            if isinstance(c, Static) and c.has_class("node-command-preview")
        ]
        assert len(previews) == 1


@pytest.mark.asyncio
async def test_pending_command_mounts_after_on_mount() -> None:
    """AC #4: ``set_command_preview`` pre-mount stashes then flushes.

    Reproduces the NODE_STARTED-arrives-before-mount race. Without
    the pending-slot pattern the fallback branch of
    ``_mount_in_contents`` (``self.mount(widget)``) would land the
    Static as a sibling of ``Collapsible.Contents`` — sibling widgets
    stay visible on collapse and violate bug-035's invariant.
    """
    row = WorkflowNodeRow(
        node_name="fetch_jd",
        node_type="kolsch/nodes/bash",
    )
    # Not yet mounted under an App — pending buffer must hold. We
    # capture the values through ``getattr`` so mypy doesn't narrow
    # the row attributes for the rest of the function (the values
    # legitimately change when ``on_mount`` fires under Pilot).
    row.set_command_preview("echo hi")
    pre_mount_pending = getattr(row, "_pending_command", None)
    pre_mount_widget = getattr(row, "_command_widget", None)
    assert pre_mount_pending == "echo hi"
    assert pre_mount_widget is None

    app = _Host()
    async with app.run_test() as pilot:
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        # `on_mount` invoked `_flush_pending_previews` — widget now lives.
        post_mount_pending = getattr(row, "_pending_command", None)
        post_mount_widget = getattr(row, "_command_widget", None)
        assert post_mount_pending is None
        assert post_mount_widget is not None
        contents = row.query_one(Collapsible.Contents)
        child_previews = [
            c for c in contents.walk_children()
            if isinstance(c, Static) and c.has_class("node-command-preview")
        ]
        assert len(child_previews) == 1


@pytest.mark.asyncio
async def test_command_survives_collapse_expand_cycle() -> None:
    """AC #4: command widget stays mounted across collapse -> expand.

    Guards against a future refactor tying command previews to the
    spec-021 buffer path. The preview is a one-shot mount driven by
    NODE_STARTED, not a replayable per-event body — so a collapse /
    expand cycle must find the same widget still present in
    ``Contents``.
    """
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(
            node_name="fetch_jd",
            node_type="kolsch/nodes/bash",
        )
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_command_preview("git log --oneline")
        await pilot.pause()

        row.collapsed = True
        await pilot.pause()
        row.collapsed = False
        await pilot.pause()

        contents = row.query_one(Collapsible.Contents)
        child_previews = [
            c for c in contents.walk_children()
            if isinstance(c, Static) and c.has_class("node-command-preview")
        ]
        assert len(child_previews) == 1, (
            "command preview must survive a collapse -> expand cycle "
            "as a one-shot mount, not a replayable per-event widget"
        )
