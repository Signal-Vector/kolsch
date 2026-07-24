"""bug-036a (bug-036d shape) — dedupe visibility tests for ``WorkflowNodeRow``.

Per-widget contract for the AC #2 gate. bug-036d migrated the
``_saw_agent_message`` flag to ``_last_assistant_text`` (empty
string means "nothing rendered"; non-empty means the ``set_output``
gate fires for agent nodes).

- Agent nodes whose sub-agent stream carried an assistant message
  render exactly one response (``AssistantProse``); the JSON-preview
  fallback in ``set_output`` is skipped.
- A late assistant-message arriving AFTER ``_on_node_completed``
  has fired ``set_state("success")`` (which freezes the row) is
  dropped — ``_last_assistant_text`` stays empty and the fallback
  ``set_output`` mounts the JSON preview. The row is never blank.
- The last-assistant-text latches BEFORE the collapsed check, so a
  row that stays collapsed through the entire run still gates
  ``set_output`` on completion.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from textual.app import App
from textual.widget import Widget
from textual.widgets import Collapsible, Static

from cli.chat.widgets.assistant_prose import AssistantProse
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow

_AppFactory = Callable[[Widget], App[None]]


@pytest.fixture
def mock_app_factory() -> _AppFactory:
    def factory(child: Widget) -> App[None]:
        class _MockApp(App[None]):
            def compose(self) -> Any:
                yield child

        return _MockApp()

    return factory


def _contents_of(row: WorkflowNodeRow) -> Collapsible.Contents:
    return row.query_one(Collapsible.Contents)


@pytest.mark.asyncio
async def test_agent_response_not_duplicated_on_normal_completion(
    mock_app_factory: _AppFactory,
) -> None:
    """Normal ordering. Assistant text first → ``set_output`` gate fires."""
    row = WorkflowNodeRow(node_name="review", node_type="kolsch/nodes/agent")
    row.collapsed = False
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        await row.mount_assistant_message("the answer is 42")
        await pilot.pause()

        row.set_state("success")
        row.set_output({"answer": "the answer is 42"})
        await pilot.pause()

        contents = _contents_of(row)
        prose = list(contents.query(AssistantProse))
        assert len(prose) == 1
        assert row._output_widget is None


@pytest.mark.asyncio
async def test_late_agent_message_after_completion_falls_back_to_set_output(
    mock_app_factory: _AppFactory,
) -> None:
    """Late assistant-message after completion: dropped, JSON preview stays."""
    row = WorkflowNodeRow(node_name="review", node_type="kolsch/nodes/agent")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        # NODE_COMPLETED lands first — ``set_state`` freezes the row,
        # ``set_output`` mounts the JSON preview.
        row.set_state("success")
        row.set_output({"summary": "42"})
        await pilot.pause()

        # Late assistant message — buffer frozen, dropped.
        await row.mount_assistant_message("the answer is 42")
        await pilot.pause()

        assert row._buffer_frozen is True
        assert row._last_assistant_text == "", (
            "buffer was frozen before the late message arrived — "
            "``_last_assistant_text`` must NOT be set"
        )
        assert row._output_widget is not None


@pytest.mark.asyncio
async def test_last_assistant_text_latches_on_collapsed_row(
    mock_app_factory: _AppFactory,
) -> None:
    """The last-assistant-text latches even when the row stays collapsed.

    bug-036d: ``mount_assistant_message`` latches
    ``_last_assistant_text`` BEFORE queueing the mount closure, so a
    row that never expanded still gates ``set_output`` on completion.
    Expanding the row after the run terminated still drains the
    queued closure (spec-021 replay-on-expand invariant survives
    the freeze); the JSON preview never mounts because the gate
    fires.
    """
    row = WorkflowNodeRow(node_name="review", node_type="kolsch/nodes/agent")
    # Row is collapsed by default.
    assert row.collapsed is True
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Assistant message arrives while collapsed — closure queued.
        await row.mount_assistant_message("the answer is 42")
        await pilot.pause()

        assert row._last_assistant_text == "the answer is 42"

        # NODE_COMPLETED with output — set_output must skip.
        row.set_state("success")
        row.set_output({"answer": "the answer is 42"})
        await pilot.pause()

        assert row._output_widget is None

        # Now expand the row — the pre-terminal closure still drains.
        row.collapsed = False
        await pilot.pause()
        await pilot.pause()

        contents = _contents_of(row)
        prose = list(contents.query(AssistantProse))
        assert len(prose) == 1
        # And the JSON preview never mounted.
        statics_from_output = [
            s for s in contents.query(Static) if s is row._output_widget
        ]
        assert not statics_from_output
