"""bug-036a — dedupe visibility tests for ``WorkflowNodeRow``.

Per-widget contract for the AC #2 gate:

- Agent nodes whose sub-agent stream carried ``agent.message``
  render exactly one response (``AssistantProse``); the JSON-preview
  fallback in ``set_output`` is skipped.
- A late ``agent.message`` arriving AFTER ``_on_node_completed`` has
  fired ``set_state("completed")`` (which freezes the buffer) is
  dropped by ``_buffer_frozen`` — so ``_saw_agent_message`` stays
  False and the fallback ``set_output`` mounts the JSON preview.
  The row never renders empty.
- The gate is set BEFORE the collapsed check, so a row that stays
  collapsed through the entire run still latches the flag and
  skips the fallback on completion.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from textual.app import App
from textual.widget import Widget
from textual.widgets import Collapsible, Static

from cli.chat.widgets.assistant_prose import AssistantProse
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId

_AppFactory = Callable[[Widget], App[None]]


def _mk_event(event_type: EventType, data: dict[str, Any]) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=ThreadId(f"th_{uuid4()}"),
        session_id=SessionId(f"ses_{uuid4()}"),
        type=event_type,
        data=data,
        actor=Actor(kind="agent", id="ag_x"),
        created_at=datetime.now(tz=UTC),
    )


@pytest.fixture
def fake_app_factory() -> _AppFactory:
    def factory(child: Widget) -> App[None]:
        class _FakeApp(App[None]):
            def compose(self) -> Any:
                yield child
        return _FakeApp()
    return factory


def _contents_of(row: WorkflowNodeRow) -> Collapsible.Contents:
    return row.query_one(Collapsible.Contents)


@pytest.mark.asyncio
async def test_agent_response_not_duplicated_on_normal_completion(
    fake_app_factory: _AppFactory,
) -> None:
    """AC #2: normal ordering. ``agent.message`` first → then
    ``_on_node_completed`` calls ``set_output`` — the gate fires,
    ``_output_widget`` stays ``None``, exactly one ``AssistantProse``.
    """
    row = WorkflowNodeRow(node_name="review", node_type="kolsch/nodes/agent")
    row.collapsed = False
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Agent's response arrives first.
        event = _mk_event(
            EventType.AGENT_MESSAGE,
            data={"content": {"text": "the answer is 42"}, "exchange_id": "ex_1"},
        )
        row.apply_sub_agent_event(event)
        await pilot.pause()

        # Then the node completes with an output payload (same content
        # as JSON — the duplicate the user reported).
        row.set_state("completed")
        row.set_output({"answer": "the answer is 42"})
        await pilot.pause()

        contents = _contents_of(row)
        prose = list(contents.query(AssistantProse))
        assert len(prose) == 1, (
            f"expected exactly one AssistantProse; got {len(prose)}"
        )
        assert row._output_widget is None, (
            "set_output must skip the JSON preview when agent.message "
            "already rendered — _output_widget should be None"
        )


@pytest.mark.asyncio
async def test_late_agent_message_after_completion_falls_back_to_set_output(
    fake_app_factory: _AppFactory,
) -> None:
    """AC #2: bug-032-shape race.

    ``_on_node_completed`` runs FIRST: it fires ``set_state("completed")``
    which freezes the buffer, then calls ``set_output(output)``. The
    late ``agent.message`` arrives via ``apply_sub_agent_event``, hits
    the ``_buffer_frozen`` guard, is dropped — ``_saw_agent_message``
    is never set. So the gate does NOT fire and ``_output_widget``
    stays mounted with the JSON preview. The row is never blank.
    """
    row = WorkflowNodeRow(node_name="review", node_type="kolsch/nodes/agent")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        # NODE_COMPLETED lands first — this mirrors what
        # ``WorkflowRenderer._on_node_completed`` does:
        # ``set_state("completed")`` (freezes buffer via ``watch_state``
        # at line 486) then ``set_output(output)``.
        row.set_state("completed")
        row.set_output({"summary": "42"})
        await pilot.pause()

        # A late agent.message arrives after completion. Buffer is
        # frozen — event is dropped, flag stays False.
        late = _mk_event(
            EventType.AGENT_MESSAGE,
            data={"content": {"text": "the answer is 42"}, "exchange_id": "ex_1"},
        )
        row.apply_sub_agent_event(late)
        await pilot.pause()

        assert row._buffer_frozen is True
        assert row._saw_agent_message is False, (
            "buffer was frozen before the late agent.message arrived — "
            "the flag must NOT be set"
        )
        assert row._output_widget is not None, (
            "set_output ran before the late message and the gate did "
            "not fire — the JSON preview must remain mounted so the "
            "row shows the response"
        )


@pytest.mark.asyncio
async def test_saw_agent_message_flag_fires_on_collapsed_row(
    fake_app_factory: _AppFactory,
) -> None:
    """AC #2: gate fires even when the row stayed collapsed.

    ``apply_sub_agent_event`` sets ``_saw_agent_message`` BEFORE the
    collapsed check, so a row that never expanded during the run
    still gates ``set_output`` on completion. The sub-agent widget
    lives in the buffer; ``_replay_buffer_to_body`` on expand mounts
    it — exactly one visible response.
    """
    row = WorkflowNodeRow(node_name="review", node_type="kolsch/nodes/agent")
    # Row is collapsed by default (workflow_node_row.py:328).
    assert row.collapsed is True
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Agent.message arrives while collapsed — NOT mounted (buffered).
        event = _mk_event(
            EventType.AGENT_MESSAGE,
            data={"content": {"text": "the answer is 42"}, "exchange_id": "ex_1"},
        )
        row.apply_sub_agent_event(event)
        await pilot.pause()

        assert row._saw_agent_message is True, (
            "the flag must latch before the collapsed check so a row "
            "that stays collapsed still gates set_output"
        )

        # NODE_COMPLETED with output — set_output must skip.
        row.set_state("completed")
        row.set_output({"answer": "the answer is 42"})
        await pilot.pause()

        assert row._output_widget is None

        # Now expand the row — buffer replays into Contents.
        row.collapsed = False
        await pilot.pause()

        contents = _contents_of(row)
        prose = list(contents.query(AssistantProse))
        assert len(prose) == 1, (
            f"after expand, exactly one AssistantProse should be "
            f"visible; got {len(prose)}"
        )
        # And the JSON preview never mounted.
        statics_from_output = [
            s for s in contents.query(Static) if s is row._output_widget
        ]
        assert not statics_from_output
