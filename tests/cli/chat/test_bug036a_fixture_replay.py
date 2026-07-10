"""bug-036a — flagship fixture-replay test.

Replays a slice of a real user session (JSONL from
``~/Documents/kolsch/sessions/``) through the real
:class:`~cli.chat.event_adapter.EventAdapter` and asserts the two
dedupe invariants:

- AC #1: no transcript-root :class:`ToolCallBlock` for the
  ``run_workflow*`` call — the tool mounts its own
  :class:`WorkflowCard`, so the additional block underneath is a
  duplicate.
- AC #2: each agent node's :class:`WorkflowNodeRow` renders its
  response exactly once — through the sub-agent stream
  (``agent.message`` → :class:`AssistantProse`), not both through
  the sub-agent stream and through
  :meth:`WorkflowNodeRow.set_output` (which would echo the same
  content as JSON).

The fixture is a real session excerpt, not synthetic. See
``tests/fixtures/sessions/extract_bug036a_fixture.py`` for the
refresh workflow.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from cli.chat.event_adapter import EventAdapter, _is_card_mounting_tool
from cli.chat.widgets.assistant_prose import AssistantProse
from cli.chat.widgets.card_widget import CardWidget
from cli.chat.widgets.tool_call_block import ToolCallBlock
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures" / "sessions" / "ses_bug036a_jd_review_excerpt.jsonl"
)


def _load_fixture() -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in _FIXTURE_PATH.read_text().splitlines()
        if line
    ]


def _event_from_row(row: dict[str, Any]) -> Event:
    """Materialise a pydantic ``Event`` from a JSONL row.

    The fixture rows are exactly what
    ``SessionStore.read_events`` yields, so the shape maps 1:1
    onto the pydantic model.
    """
    return Event(
        event_id=EventId(row["event_id"]),
        parent_event_id=row.get("parent_event_id"),
        thread_id=ThreadId(row["thread_id"]),
        session_id=SessionId(row["session_id"]),
        type=EventType(row["type"]),
        data=row.get("data") or {},
        actor=Actor(**(row.get("actor") or {"kind": "system"})),
        created_at=row["created_at"],
        trace_id=row.get("trace_id"),
    )


class _StubTranscriptApp(App[None]):
    """Minimal app that only owns a ``#transcript`` VerticalScroll.

    The adapter mounts CardWidgets into this container via
    ``_locate_transcript`` → ``query_one("#transcript")`` and the
    workflow-progress fan-out iterates ``_card_widgets`` for
    renderers, so the real CardWidget + WorkflowRenderer wiring
    happens end-to-end without booting the full ChatApp.
    """

    def __init__(self) -> None:
        super().__init__()
        self.mounted_tool_calls: list[tuple[str, ...]] = []

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript")

    # ChatApp surface the adapter calls.

    async def mount_user_message(self, text: str, timestamp: Any) -> None:
        _ = text, timestamp

    async def mount_assistant_message(self, text: str) -> None:
        _ = text

    async def mount_assistant_streaming_widget(self) -> Any:
        class _P:
            async def append(self, text: str) -> None:
                _ = text
        return _P()

    async def mount_tool_call(
        self,
        tool_name: str,
        args: str,
        body: str,
        *,
        failed: bool = False,
        recovery_hint: str | None = None,
    ) -> None:
        """Record every mount at transcript root.

        AC #1 assertion: none of these should be for a
        card-mounting tool.
        """
        _ = failed, recovery_hint
        self.mounted_tool_calls.append((tool_name, args, body))

    async def mount_notice(self, text: str) -> None:
        _ = text

    async def mount_exchange_failed(self, message: str) -> None:
        _ = message

    async def mount_approval_block(self, *args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        return None

    async def set_turn_verb(self, verb: str, *, target: str = "") -> None:
        _ = verb, target

    def clear_awaiting_approval(self) -> None:
        return None

    def last_assistant_text(self) -> str:
        return ""

    def set_last_assistant_text(self, text: str) -> None:
        _ = text


def test_predicate_catches_run_workflow_and_virtuals() -> None:
    """Guard: the predicate must catch bug-028's virtual tool names.

    Without the ``startswith("run_workflow_")`` branch every
    per-workflow virtual (the way Kimi actually invokes workflows)
    would slip past the suppression.
    """
    assert _is_card_mounting_tool("run_workflow")
    assert _is_card_mounting_tool(
        "run_workflow_andre_workflows_job_description_review",
    )
    assert _is_card_mounting_tool("run_workflow_kolsch_workflows_hello")
    assert not _is_card_mounting_tool("bash")
    assert not _is_card_mounting_tool("read_file")
    # The predicate is name-shape-based: any ``run_workflow_*`` name
    # is assumed card-mounting. That matches how bug-028 emits typed
    # virtuals. Tools that share the prefix without opening a card
    # (none in the current bundle) would need their own path.


@pytest.mark.asyncio
async def test_fixture_replay_dedupe_invariants() -> None:
    """Replay the fixture; assert AC #1 + AC #2 invariants hold.

    Steps:
    1. Boot a stub app (``#transcript`` container only).
    2. Instantiate a real ``EventAdapter``.
    3. For each fixture event, ``await adapter.handle(event)``.
    4. Expand every mounted ``WorkflowNodeRow`` so the assertions
       can see body-mounted widgets (rows are constructed
       collapsed at line 328 of ``workflow_node_row.py``).
    5. Assert:
       - No standalone ``ToolCallBlock`` mounted at transcript
         root whose ``tool_name`` matches
         ``_is_card_mounting_tool``.
       - Each agent node's row has exactly one
         ``AssistantProse`` and ``_output_widget is None``.
    """
    fixture = _load_fixture()
    app = _StubTranscriptApp()
    async with app.run_test() as pilot:
        adapter = EventAdapter(app)
        for row in fixture:
            event = _event_from_row(row)
            await adapter.handle(event)
        await pilot.pause()

        # AC #1: transcript-root ToolCallBlock suppression.
        for tool_name, _args, _body in app.mounted_tool_calls:
            assert not _is_card_mounting_tool(tool_name), (
                f"transcript-root ToolCallBlock mounted for "
                f"card-mounting tool {tool_name!r}; AC #1 violated"
            )

        # Also verify via the widget tree: no ToolCallBlock with a
        # matching tool_name inside the transcript root (excluding
        # any that landed inside a WorkflowNodeRow's Contents).
        workflow_cards = [
            w for w in pilot.app.query(CardWidget)
            if getattr(w, "kind", None) == "workflow"
        ]
        assert workflow_cards, (
            "the fixture must open a workflow card — otherwise the "
            "AC #1 test surface is trivially vacuous"
        )

        # Expand every node row so sub-agent widgets replay into
        # Contents. Rows are collapsed by default.
        rows = list(pilot.app.query(WorkflowNodeRow))
        assert rows, "no WorkflowNodeRow mounted"
        for r in rows:
            r.collapsed = False
        await pilot.pause()

        # AC #2: agent rows render one response, no set_output.
        agent_rows = [
            r for r in rows if r._node_type == "kolsch/nodes/agent"
        ]
        assert agent_rows, (
            "the fixture must contain at least one agent-type node — "
            "otherwise the AC #2 surface is trivially vacuous"
        )
        for node_row in agent_rows:
            prose = list(node_row.query(AssistantProse))
            assert len(prose) == 1, (
                f"row {node_row._node_name!r} should render exactly "
                f"one AssistantProse; got {len(prose)}"
            )
            assert node_row._output_widget is None, (
                f"row {node_row._node_name!r}: set_output must be "
                f"gated when agent.message already rendered — "
                f"_output_widget should be None"
            )

        # Any transcript-root ToolCallBlock is fine if it's not a
        # card-mounting tool — verify no card-mounting tool leaked.
        transcript_blocks = [
            w for w in pilot.app.query(ToolCallBlock)
            # Skip blocks that live inside a WorkflowNodeRow's body.
            if not any(isinstance(a, WorkflowNodeRow) for a in w.ancestors)
        ]
        for block in transcript_blocks:
            # ToolCallBlock stores the tool name on construction —
            # scan its title/body for the ``run_workflow`` marker.
            for attr in ("_tool_name", "tool_name"):
                name = getattr(block, attr, None)
                if isinstance(name, str) and _is_card_mounting_tool(name):
                    msg = (
                        f"transcript-root ToolCallBlock for "
                        f"card-mounting tool {name!r} found in tree"
                    )
                    raise AssertionError(msg)
