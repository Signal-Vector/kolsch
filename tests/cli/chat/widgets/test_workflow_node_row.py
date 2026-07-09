"""spec-013 Test 2 — `WorkflowNodeRow` state machine + CSS.

Seven passes — one per state in the state machine.

bug-031: ``starting`` and ``running`` states now render a rotating
braille spinner frame instead of the static ``⠋`` / ``▸`` glyphs so
the row visibly ticks during long-running nodes. Terminal states
still use the static ``STATE_GLYPHS`` entries.
"""

from __future__ import annotations

import pytest

from cli.chat.widgets.workflow_node_row import (
    _SPINNER_FRAMES,
    WorkflowNodeRow,
)


@pytest.mark.asyncio
async def test_initial_starting_state(fake_app_factory) -> None:
    row = WorkflowNodeRow(node_name="x")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        # bug-031: wait past the 0.08s spinner interval so the tick
        # replaces the ``__init__`` placeholder glyph.
        await pilot.pause(0.1)
        assert row.state == "starting"
        # bug-031: the row now renders a braille spinner frame while
        # in an animated state — assert one of the eight frames is
        # present rather than the pre-bug-031 static ``⠋``.
        assert any(frame in row.title for frame in _SPINNER_FRAMES)
        assert "x" in row.title
        # No CSS class applied for the default state.
        assert "-completed" not in row.classes
        assert "-failed" not in row.classes


@pytest.mark.asyncio
async def test_running_state_no_css(fake_app_factory) -> None:
    row = WorkflowNodeRow(node_name="x")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_state("running")
        # bug-031: wait past the 0.08s spinner interval so the tick
        # rewrites the title with a spinner frame.
        await pilot.pause(0.1)
        assert row.state == "running"
        # bug-031: ``running`` is an animated state — spinner frame,
        # not the pre-bug-031 static ``▸``.
        assert any(frame in row.title for frame in _SPINNER_FRAMES)
        # `running` is a default-palette state — no CSS class.
        assert "-completed" not in row.classes
        assert "-failed" not in row.classes


@pytest.mark.asyncio
async def test_completed_state_with_details(fake_app_factory) -> None:
    row = WorkflowNodeRow(node_name="x")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_state("completed", details="42 files")
        await pilot.pause()
        assert row.state == "completed"
        assert "✓" in row.title
        assert "-completed" in row.classes
        assert "42 files" in row.title


@pytest.mark.asyncio
async def test_failed_state_with_truncated_error(fake_app_factory) -> None:
    row = WorkflowNodeRow(node_name="x")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_state("failed", error_preview="RegistryError: bad")
        await pilot.pause()
        assert row.state == "failed"
        assert "✗" in row.title
        assert "-failed" in row.classes
        assert "RegistryError" in row.title


@pytest.mark.asyncio
async def test_paused_state(fake_app_factory) -> None:
    row = WorkflowNodeRow(node_name="x")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_state("paused")
        await pilot.pause()
        assert row.state == "paused"
        assert "⚠" in row.title
        assert "-paused" in row.classes


@pytest.mark.asyncio
async def test_cancelling_state(fake_app_factory) -> None:
    row = WorkflowNodeRow(node_name="x")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_state("cancelling")
        await pilot.pause()
        assert row.state == "cancelling"
        assert "⧗" in row.title
        assert "-cancelling" in row.classes


@pytest.mark.asyncio
async def test_pruned_state(fake_app_factory) -> None:
    row = WorkflowNodeRow(node_name="x")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_state("pruned")
        await pilot.pause()
        assert row.state == "pruned"
        assert "─" in row.title
        assert "-pruned" in row.classes
