"""bug-035 flagship — live workflow card UX end-to-end.

Boots a real ``ChatApp`` under Pilot, drives an actual workflow with
sub-agent events, and asserts the three user-reported invariants:

1. **Collapse hides the row body.** After collapsing a
   ``WorkflowNodeRow``, sub-agent widgets are no longer visible —
   i.e. every mounted widget lives inside the ``Collapsible.Contents``
   slot, which the CSS rule ``.-collapsed > Contents { display: none
   }`` hides. Before bug-035, ``self.mount(widget)`` mounted as a
   sibling of ``Contents`` and stayed visible when collapsed.

2. **``USER_MESSAGE`` produces no ``UserChatbox``.** Inside a
   workflow node, USER_MESSAGE is the scheduler's invocation payload,
   not a real user turn. The row body should only contain
   ``AssistantProse`` + ``ToolCallBlock`` — the semantically correct
   widgets.

3. **Workflow card stays open after ``success``.** Before bug-035,
   ``CardWidget.apply_close`` auto-collapsed after 1s on ``success``
   for every card kind, hiding the workflow timeline behind a title.
   Post-bug-035, workflow cards skip the auto-collapse timer.

The harness mirrors ``test_sub_agent_events_stream_to_node_row.py``
(spec-020 flagship) — same mock executor + ``chat_app`` fixture.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from textual.widgets import Collapsible

from cli.chat.app import ChatApp
from cli.chat.services.workflows_impl import WorkflowsImpl
from cli.chat.widgets.assistant_prose import AssistantProse
from cli.chat.widgets.card_body.workflow import WorkflowRenderer
from cli.chat.widgets.card_widget import CardWidget
from cli.chat.widgets.user_chatbox import UserChatbox
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import AgentId, EventId, SessionId, ThreadId
from tools.decorator import ToolContext, get_tool_definition
from tools.discovery import discover_tools_in_file
from tools.dispatch import _invoke_tool
from workflows.events import (
    WorkflowNodeCompletedEvent,
    WorkflowNodeStartedEvent,
    WorkflowRunCompletedEvent,
    WorkflowRunStartedEvent,
)
from workflows.ids import InvocationId, TraceId, WorkflowRunId
from workflows.run import RunDirectory, WorkflowRunStatus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from cli.chat.widgets.workflow_node_row import WorkflowNodeRow

_BUNDLE_TOOLS_DIR = Path(__file__).resolve().parents[3] / "bundles" / "kolsch" / "tools"


def _load_tool(filename: str, tool_name: str) -> Callable[..., Awaitable[Any]]:
    path = _BUNDLE_TOOLS_DIR / filename
    for fn in discover_tools_in_file(path):
        if get_tool_definition(fn).name == tool_name:
            return fn
    msg = f"tool {tool_name!r} not found in {path}"
    raise LookupError(msg)


@pytest.fixture
def run_workflow_tool() -> Callable[..., Awaitable[Any]]:
    return _load_tool("run_workflow.py", "run_workflow")


class _MockAuthz:
    def register_prompt_callback(
        self,
        callback: Any,
        *,
        on_rule_added: Any = None,
    ) -> None:
        _ = callback, on_rule_added

    def clear_prompt_callback(self) -> None:
        return None


class _MockEngine:
    async def execute_stream(self, config: Any, ctx: Any) -> Any:
        _ = config, ctx
        if False:  # pragma: no cover
            yield


class _MockConfig:
    name = "TestAgent"
    agent_id = "ag_mock"
    model = None


class _MockBundle:
    def __init__(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


@pytest.fixture
async def chat_app(tmp_path: Path) -> Any:
    """Boot a `ChatApp` under Pilot with the full live wire stack."""
    from bus.in_process import InProcessEventBus
    from store import SessionStore, SqliteBackend

    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    store = SessionStore(root=tmp_path, backend=backend)
    session = await store.create_session("ag_mock", name="c")
    threads = await store.list_threads(session.session_id)

    class _R:
        def __init__(self) -> None:
            self.store = store
            self.authz = _MockAuthz()
            self.engine = _MockEngine()
            self.root = tmp_path
            self.bus = InProcessEventBus()

    bundle = _MockBundle(
        runtime=_R(),
        config=_MockConfig(),
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=False,
    )
    app = ChatApp(
        session=bundle,  # type: ignore[arg-type]
        engine=bundle.runtime.engine,
        full_history=False,
    )
    try:
        yield app
    finally:
        await backend.close()


async def _invoke_tool_via_pilot(
    pilot: Any,
    tool_fn: Callable[..., Awaitable[Any]],
    args_dict: dict[str, Any],
) -> Any:
    app = pilot.app
    defn = get_tool_definition(tool_fn)
    validated_args = defn.args_schema.model_validate(args_dict)
    registry = app._service_registry
    ctx = ToolContext(
        agent_id=AgentId(app.session.config.agent_id),
        session_id=SessionId(str(app.session.session_id)),
        thread_id=ThreadId(str(app.session.thread_id)),
        principal="user",
        secrets={},
        cards=registry.cards if registry is not None else None,
        prompts=registry.prompts if registry is not None else None,
        workflows=registry.workflows if registry is not None else None,
        session=registry.session if registry is not None else None,
    )
    return await _invoke_tool(tool_fn, validated_args, ctx)


def _mk_session_event(
    *,
    event_type: EventType,
    trace_id: str,
    data: dict[str, Any] | None = None,
) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid.uuid4()}"),
        parent_event_id=None,
        thread_id=ThreadId(f"th_{uuid.uuid4()}"),
        session_id=SessionId(f"ses_{uuid.uuid4()}"),
        type=event_type,
        data=dict(data or {}),
        actor=Actor(kind="agent", id="ag_mock"),
        created_at=datetime.now(UTC),
        trace_id=trace_id,
    )


class _MockExecutor:
    """Fires RUN_STARTED → NODE_STARTED → sub-agent events → NODE_COMPLETED → RUN_COMPLETED."""

    def __init__(
        self,
        *,
        observer: Callable[[Any], None],
        sub_agent_observer: Any,
        cancel_token: Any,
        per_event_delay: float = 0.02,
        pre_sub_agent_delay: float = 2.0,
    ) -> None:
        self._observer = observer
        self._sub_agent_observer = sub_agent_observer
        self._cancel_token = cancel_token
        self._delay = per_event_delay
        self._pre_sub_agent_delay = pre_sub_agent_delay
        self.run_id = WorkflowRunId(f"wrk_{uuid4()}")
        self.trace_id = TraceId(f"tr_{uuid4()}")
        self.invocation_id = InvocationId(f"inv_{uuid4()}")
        self.node_name = "classify"

    async def run(self) -> WorkflowRunStatus:
        self._observer(
            WorkflowRunStartedEvent(
                workflow_run_id=self.run_id,
                trace_id=self.trace_id,
            )
        )
        await asyncio.sleep(self._delay)
        self._observer(
            WorkflowNodeStartedEvent(
                workflow_run_id=self.run_id,
                trace_id=self.trace_id,
                invocation_id=self.invocation_id,
                node_name=self.node_name,
            )
        )
        await asyncio.sleep(self._pre_sub_agent_delay)
        if self._sub_agent_observer is not None:
            for evt in (
                _mk_session_event(
                    event_type=EventType.USER_MESSAGE,
                    trace_id=str(self.trace_id),
                    data={"content": {"text": "classify me"}},
                ),
                _mk_session_event(
                    event_type=EventType.TOOL_CALLED,
                    trace_id=str(self.trace_id),
                    data={
                        "tool_name": "bash",
                        "arguments": {"command": "ls -la"},
                        "tool_call_id": "tc_1",
                    },
                ),
                _mk_session_event(
                    event_type=EventType.AGENT_MESSAGE,
                    trace_id=str(self.trace_id),
                    data={"content": {"text": "here is my response"}},
                ),
            ):
                # spec-103 approach A: emitting run's id first.
                self._sub_agent_observer(
                    str(self.run_id),
                    self.node_name,
                    str(self.invocation_id),
                    evt,
                )
                await asyncio.sleep(self._delay)
        self._observer(
            WorkflowNodeCompletedEvent(
                workflow_run_id=self.run_id,
                trace_id=self.trace_id,
                invocation_id=self.invocation_id,
                node_name=self.node_name,
            )
        )
        await asyncio.sleep(self._delay)
        self._observer(
            WorkflowRunCompletedEvent(
                workflow_run_id=self.run_id,
                trace_id=self.trace_id,
            )
        )
        return WorkflowRunStatus.SUCCEEDED


def _install_mock(impl: WorkflowsImpl, tmp_path: Path) -> dict[str, _MockExecutor]:
    holder: dict[str, _MockExecutor] = {}

    def _factory(
        ref_in: str,
        inputs: dict[str, Any],
        observer: Callable[[Any], None],
        cancel_token: Any,
        sub_agent_observer: Any = None,
    ) -> tuple[_MockExecutor, RunDirectory]:
        _ = (ref_in, inputs)
        run_dir = RunDirectory(root=tmp_path / "runs" / f"wrk_{uuid.uuid4()}")
        run_dir.root.mkdir(parents=True, exist_ok=True)
        mock = _MockExecutor(
            observer=observer,
            sub_agent_observer=sub_agent_observer,
            cancel_token=cancel_token,
        )
        holder["mock"] = mock
        return mock, run_dir

    impl._executor_factory = _factory
    return holder


async def _drive_workflow(
    pilot: Any,
    run_workflow_tool: Callable[..., Awaitable[Any]],
) -> tuple[WorkflowRenderer, WorkflowNodeRow, asyncio.Task[Any], CardWidget]:
    """Kick off a workflow and wait for the node row to mount + populate.

    Returns (renderer, node_row, tool_task, card_widget). Caller
    ``await`` s ``tool_task`` when they need the workflow to finish.
    """
    workflows_impl = pilot.app._service_registry.workflows
    assert isinstance(workflows_impl, WorkflowsImpl)
    _install_mock(workflows_impl, pilot.app.session.root)

    tool_task = asyncio.create_task(
        _invoke_tool_via_pilot(
            pilot,
            run_workflow_tool,
            {"ref": "kolsch/workflows/hello-agent", "inputs": {}},
        )
    )
    card: CardWidget | None = None
    for _ in range(80):
        await pilot.pause()
        cards = [
            w
            for w in pilot.app.query(CardWidget)
            if getattr(w, "kind", None) == "workflow"
        ]
        if cards:
            card = cards[0]
            break
        await asyncio.sleep(0.05)
    assert card is not None
    assert isinstance(card._body, WorkflowRenderer)
    renderer = card._body

    for _ in range(80):
        await pilot.pause()
        if renderer._node_rows:
            break
        await asyncio.sleep(0.05)
    assert renderer._node_rows
    node_row = next(iter(renderer._node_rows.values()))
    return renderer, node_row, tool_task, card


# ---------------------------------------------------------------------------
# The three flagship scenarios.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collapse_hides_sub_agent_widgets_in_live_app(
    chat_app: Any,
    run_workflow_tool: Callable[..., Awaitable[Any]],
) -> None:
    """User complaint #1: collapsing the row must hide its content.

    Before bug-035, sub-agent widgets mounted as siblings of
    Collapsible.Contents and stayed visible when collapsed. Post-fix,
    every mount targets Contents so the CSS ``.-collapsed > Contents``
    rule governs visibility.
    """
    async with chat_app.run_test() as pilot:
        await pilot.pause()
        _, node_row, tool_task, _ = await _drive_workflow(
            pilot,
            run_workflow_tool,
        )

        # Expand the row so events land visibly (Coordinator #3 lock
        # drops widgets while collapsed; buffer replays on re-expand).
        node_row.collapsed = False
        await pilot.pause()

        # bug-036d: parity with main-chat handlers — TOOL_CALLED
        # captures args only; a ToolCallBlock lands on TOOL_RESULT.
        # The mock executor fires USER_MESSAGE + TOOL_CALLED +
        # AGENT_MESSAGE (no TOOL_RESULT), so we expect UserChatbox
        # + AssistantProse only.
        for _ in range(200):
            await pilot.pause()
            seen = {
                type(w)
                for w in node_row.query("*")
                if type(w) in {UserChatbox, AssistantProse}
            }
            if seen == {UserChatbox, AssistantProse}:
                break
            await asyncio.sleep(0.05)
        assert list(node_row.query(AssistantProse)), "AssistantProse missing"

        # -- Both widgets are inside Contents (bug-035 invariant) --
        contents = node_row.query_one(Collapsible.Contents)
        prose_in_contents = list(contents.query(AssistantProse))
        user_in_contents = list(contents.query(UserChatbox))
        assert prose_in_contents, "AssistantProse not inside Contents"
        assert user_in_contents, "UserChatbox not inside Contents"

        # -- With the row EXPANDED, Contents is visible --
        assert not node_row.has_class("-collapsed"), (
            "row shouldn't carry -collapsed class while expanded"
        )
        assert contents.styles.display != "none", (
            "Contents.display should NOT be 'none' when expanded"
        )

        # -- Collapse the row and reflow --
        node_row.collapsed = True
        # Multiple pauses so Textual runs the reactive → CSS →
        # layout pipeline. Real users take longer than one frame.
        for _ in range(20):
            await pilot.pause()

        # -- Assert Contents is hidden by CSS --
        assert node_row.has_class("-collapsed"), (
            "row should carry -collapsed class after collapse=True"
        )
        # The load-bearing invariant: the containing element that
        # ACTUALLY holds the widgets is display: none.
        assert contents.styles.display == "none", (
            f"Contents.display should be 'none' after collapse; "
            f"got {contents.styles.display!r}. Widgets in Contents: "
            f"{[type(w).__name__ for w in contents.walk_children()]}"
        )

        # Clean up: cancel the still-running tool task.
        tool_task.cancel()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(tool_task, timeout=5.0)


@pytest.mark.asyncio
async def test_user_message_produces_userchatbox_widget_in_live_app(
    chat_app: Any,
    run_workflow_tool: Callable[..., Awaitable[Any]],
) -> None:
    """bug-036d: USER_MESSAGE inside a workflow row DOES mount a UserChatbox.

    Reverses bug-035's suppression: the row is a full chat container
    now. The scheduler's invocation payload gets its own UserChatbox
    at the top of the body (Mockup 3), so a reader can see what
    inputs the sub-agent was called with.
    """
    async with chat_app.run_test() as pilot:
        await pilot.pause()
        _, node_row, tool_task, _ = await _drive_workflow(
            pilot,
            run_workflow_tool,
        )
        node_row.collapsed = False
        for _ in range(200):
            await pilot.pause()
            if list(node_row.query(UserChatbox)):
                break
            await asyncio.sleep(0.05)

        assert list(node_row.query(UserChatbox)), (
            "bug-036d: UserChatbox SHOULD mount inside a workflow "
            "row for the sub-agent's invocation payload"
        )

        tool_task.cancel()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(tool_task, timeout=5.0)


@pytest.mark.asyncio
async def test_workflow_card_stays_expanded_after_success(
    chat_app: Any,
    run_workflow_tool: Callable[..., Awaitable[Any]],
) -> None:
    """User complaint #3: after success, workflow info must remain visible.

    Before bug-035, ``CardWidget.apply_close`` scheduled a 1s
    auto-collapse timer for every ``success`` card, which hid the
    workflow's node timeline behind a "✓" title. Bug-035 skips the
    timer when ``self.kind == 'workflow'``.
    """
    async with chat_app.run_test() as pilot:
        await pilot.pause()
        _, _, tool_task, card = await _drive_workflow(
            pilot,
            run_workflow_tool,
        )

        # Let the workflow finish naturally.
        try:
            await asyncio.wait_for(tool_task, timeout=15.0)
        except TimeoutError:
            pytest.fail("workflow tool did not complete within 15s")

        # Wait well past the 1s auto-collapse timer that
        # ``CardWidget.apply_close`` schedules for non-workflow kinds.
        # 40 * 50ms = 2s; leaves plenty of slack over the 1s cadence.
        for _ in range(40):
            await pilot.pause()
            await asyncio.sleep(0.05)

        assert not card.collapsed, (
            "workflow card should stay EXPANDED after success — "
            "bug-035 skips the auto-collapse timer for workflow "
            "kinds so the user can still see the node timeline"
        )
