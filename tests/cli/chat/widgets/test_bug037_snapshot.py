"""bug-037 — snapshot goldens for Mockups 3, 4, 5.

Reviewer #1: Mockup 7 (map iterations) dropped — no live event
stream to replay; asserted via AC #9 sub-test 10 instead.

Goldens live under `tests/snapshots/bug037/`. To regenerate:

    SNAPSHOT_UPDATE=1 pytest tests/cli/chat/widgets/test_bug037_snapshot.py
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from cli.chat.widgets.node_section import NodeSection
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from tests.cli.chat.snapshot_helpers import assert_snapshot_matches

_APP_SIZE = (140, 60)


class _Host(App[None]):
    CSS = """
    Screen { height: 100vh; }
    #slot { height: 1fr; width: 100%; }
    """

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="slot")


async def _expand_all(row: WorkflowNodeRow) -> None:
    """Expand the row + every NodeSection inside it."""
    row.collapsed = False
    for section in row.query(NodeSection):
        section.collapsed = False


# ---------------------------------------------------------------------------
# Mockup 3 — bash sections expanded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_mockup3_bash_expanded() -> None:
    """Bash row with input / output / exit sections expanded."""
    app = _Host()
    async with app.run_test(size=_APP_SIZE) as pilot:
        row = WorkflowNodeRow(
            node_name="fetch_jd",
            node_type="kolsch/nodes/bash",
        )
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_bound_inputs({"url": "https://example.com/jd"})
        row.set_command_preview("curl {{ inputs.url }}")
        await pilot.pause()
        row.set_output(
            {
                "stdout": "Senior CTO - Blockchain Startup\n\nWe are building...",
                "stderr": "",
                "exit_code": 0,
            }
        )
        row.set_state("completed")
        row.set_completed_at(duration_ms=8200.0)
        await pilot.pause()

        await _expand_all(row)
        await pilot.pause()
        await pilot.pause()

        assert_snapshot_matches(pilot.app, "bug037/mockup3_bash_expanded")


# ---------------------------------------------------------------------------
# Mockup 4 — agent row expanded, sections collapsed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_mockup4_agent_collapsed_sections() -> None:
    """Agent row expanded; input + response sections stay collapsed."""
    app = _Host()
    async with app.run_test(size=_APP_SIZE) as pilot:
        row = WorkflowNodeRow(
            node_name="headhunter_review",
            node_type="kolsch/nodes/agent",
        )
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        await row.mount_user_message(
            "job_description: ...",
            timestamp=datetime(2026, 7, 9, tzinfo=UTC),
            content={"job_description": "Senior CTO..."},
        )
        await row.mount_assistant_message(
            "the model response",
            event_id="r1",
        )
        await pilot.pause()
        row.set_state("completed")
        row.collapsed = False
        await pilot.pause()
        await pilot.pause()

        assert_snapshot_matches(
            pilot.app,
            "bug037/mockup4_agent_collapsed_sections",
        )


# ---------------------------------------------------------------------------
# Mockup 5 — deduper: 3 input sections + response expanded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_mockup5_deduper_input_split() -> None:
    """Deduper row with 3 input sections + response section, all expanded."""
    app = _Host()
    async with app.run_test(size=_APP_SIZE) as pilot:
        row = WorkflowNodeRow(
            node_name="deduper",
            node_type="kolsch/nodes/agent",
        )
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        await row.mount_user_message(
            "flat dedup input",
            timestamp=datetime(2026, 7, 9, tzinfo=UTC),
            content={
                "headhunter_review": "scoring array from headhunter",
                "hiring_manager_review": "scoring array from hiring_manager",
                "cto_review": "scoring array from cto",
            },
        )
        await row.mount_assistant_message(
            '```json\n[{"requirement": "Blockchain", "average_score": 9.33}]\n```',
            event_id="r1",
        )
        await pilot.pause()
        row.set_state("completed")
        await _expand_all(row)
        await pilot.pause()
        await pilot.pause()

        assert_snapshot_matches(
            pilot.app,
            "bug037/mockup5_deduper_input_split",
        )
