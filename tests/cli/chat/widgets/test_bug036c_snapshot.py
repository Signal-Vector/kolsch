"""bug-036c — rendered-output snapshot tests (Layer 2, AC #7).

Consumes the ``tests/cli/chat/snapshot_helpers.py`` module bug-036a
introduced. Four goldens under ``tests/snapshots/bug036c/``:

- ``header_top_level`` — top-level RUN_STARTED renders ref + inputs
  + progress bar (3-line chrome, no ``↳ from`` breadcrumb).
- ``header_nested`` — same but with ``parent_ref`` set; first line
  flattens to ``↳ from <parent> · <ref>``.
- ``bash_command_visible`` — expanded bash row shows the
  ``↳ command: <template>`` preview.
- ``bash_command_hidden_when_collapsed`` — same row collapsed —
  proves the widget landed inside ``Collapsible.Contents`` and is
  hidden by CSS on collapse (bug-035 mount-tree invariant).

Refresh goldens explicitly:

    SNAPSHOT_UPDATE=1 pytest tests/cli/chat/widgets/test_bug036c_snapshot.py
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from cli.chat.widgets.card_body.workflow import WorkflowRenderer
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from tests.cli.chat.snapshot_helpers import assert_snapshot_matches

_APP_SIZE = (200, 100)


class _SnapshotHost(App[None]):
    """Minimal app with a full-height ``#slot`` for the snapshot capture."""

    CSS = """
    Screen { height: 100vh; }
    #slot { height: 1fr; width: 100%; }
    """

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="slot")


@pytest.mark.asyncio
async def test_snapshot_header_shows_ref_and_inputs_top_level() -> None:
    """Golden: top-level header — ref, inputs, progress (no breadcrumb)."""
    app = _SnapshotHost()
    async with app.run_test(size=_APP_SIZE) as pilot:
        renderer = WorkflowRenderer(id="wf")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(renderer)
        await pilot.pause()

        renderer._on_run_started(
            {
                "run_id": "wrk_snapshot_top",
                "workflow_ref": "acme/workflows/jd_review",
                "inputs": {"url": "https://example.com/jd"},
                "ts": "2026-07-09T00:00:00Z",
            }
        )
        await pilot.pause()

        assert_snapshot_matches(pilot.app, "bug036c/header_top_level")


@pytest.mark.asyncio
async def test_snapshot_header_flattens_parent_ref_when_nested() -> None:
    """Golden: nested header — ``↳ from <parent> · <ref>`` first line."""
    app = _SnapshotHost()
    async with app.run_test(size=_APP_SIZE) as pilot:
        renderer = WorkflowRenderer(id="wf", parent_ref="parent/wf/outer")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(renderer)
        await pilot.pause()

        renderer._on_run_started(
            {
                "run_id": "wrk_snapshot_nested",
                "workflow_ref": "child/wf/inner",
                "inputs": {"url": "https://example.com"},
                "ts": "2026-07-09T00:00:00Z",
            }
        )
        await pilot.pause()

        assert_snapshot_matches(pilot.app, "bug036c/header_nested")


@pytest.mark.asyncio
async def test_snapshot_bash_row_shows_command() -> None:
    """Golden: expanded bash row body shows ``↳ command: <template>``."""
    app = _SnapshotHost()
    async with app.run_test(size=_APP_SIZE) as pilot:
        row = WorkflowNodeRow(
            node_name="fetch_jd",
            node_type="kolsch/nodes/bash",
        )
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_command_preview("git log --oneline {{ inputs.since }}")
        row.collapsed = False
        await pilot.pause()

        assert_snapshot_matches(pilot.app, "bug036c/bash_command_visible")


@pytest.mark.asyncio
async def test_snapshot_bash_row_collapsed_hides_command() -> None:
    """Golden: collapsed bash row hides the command (mount-tree invariant)."""
    app = _SnapshotHost()
    async with app.run_test(size=_APP_SIZE) as pilot:
        row = WorkflowNodeRow(
            node_name="fetch_jd",
            node_type="kolsch/nodes/bash",
        )
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_command_preview("git log --oneline {{ inputs.since }}")
        # Row stays collapsed by default.
        await pilot.pause()

        assert_snapshot_matches(
            pilot.app,
            "bug036c/bash_command_hidden_when_collapsed",
        )
