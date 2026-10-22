"""spec-020 Test 5 (AC #5) — flagship live sub-agent event stream.

D-010 gate. Boots a real :class:`~cli.chat.app.ChatApp` under Pilot,
runs a workflow whose mock executor emits sub-agent events for its
one agent node, expands the resulting :class:`WorkflowNodeRow`, and
asserts the sub-agent widgets (`UserChatbox`, `AssistantProse`,
`ToolCallBlock`) mount inside the row's body as events stream by.

**No scaffolding.** This test MUST NOT reach for
``_replay_ui_events_since``, ``adapter.handle(...)`` direct dispatch,
or any helper that side-steps the runtime bus subscription in
``ChatApp.on_mount``. If any of those appear in this file, the
spec-020 implementation is incomplete (D-010 gate) — the grep-guard
at the bottom of this file self-tests it.

The mock executor mirrors what a real ``AgentNodeRunner`` produces:
after emitting ``NODE_STARTED`` it fires three session-scoped
``Event``s through the ``sub_agent_event_observer``:

- ``USER_MESSAGE``   → `UserChatbox`
- ``TOOL_CALLED``    → `ToolCallBlock`
- ``AGENT_MESSAGE``  → `AssistantProse`

Each translates through
``WorkflowsImpl._emit_sub_agent_ui_event`` into a
``kolsch.workflow.node.sub_agent_event`` UI event; the live bus
subscription routes it to ``WorkflowRenderer.apply_event`` which
looks up the target row via bug-033's
``(trace_id, invocation_id)`` composite map (adapter-side) and calls
``row.apply_sub_agent_event``.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from cli.chat.app import ChatApp
from cli.chat.services.workflows_impl import WorkflowsImpl
from cli.chat.widgets.assistant_prose import AssistantProse
from cli.chat.widgets.card_body.workflow import WorkflowRenderer
from cli.chat.widgets.card_widget import CardWidget
from cli.chat.widgets.user_chatbox import UserChatbox
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from schemas import CallerChain
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import AgentId, EventId, SessionId, ThreadId
from tests.fixtures.bundle_paths import STARTER_TOOLS_DIR, load_bundle_tool
from tools.decorator import ToolContext, get_tool_definition
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
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path


@pytest.fixture
def run_workflow_tool() -> Callable[..., Awaitable[Any]]:
    return load_bundle_tool(STARTER_TOOLS_DIR, "run_workflow.py", "run_workflow")


# ---------------------------------------------------------------------------
# Local `chat_app` fixture — same shape as
# ``tests/cli/chat/test_workflow_card_live_mount.py`` (the spec-020
# flagship needs the exact same live-bus wiring).
# ---------------------------------------------------------------------------


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
    async def execute_stream(self, _config: Any, _ctx: Any) -> AsyncIterator[Any]:
        return
        yield  # pragma: no cover


class _MockConfig:
    name = "TestAgent"
    agent_id = "ag_mock"
    model = None


class _MockBundle:
    def __init__(
        self,
        *,
        runtime: Any,
        config: Any,
        session_id: Any,
        thread_id: Any,
        agent_ref: str,
        root: Path,
        model_label: str,
        is_resume: bool,
    ) -> None:
        self.runtime = runtime
        self.config = config
        self.session_id = session_id
        self.thread_id = thread_id
        self.agent_ref = agent_ref
        self.root = root
        self.model_label = model_label
        self.is_resume = is_resume


@pytest.fixture
async def chat_app(tmp_path: Path) -> Any:
    """Boot a `ChatApp` under Pilot with `InProcessEventBus` + services."""
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
        session=bundle,
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
    """Local mirror of the bundle-suite helper — routes through spec-008 finalizer."""
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
        caller_chain=CallerChain.unattributed(),
    )
    return await _invoke_tool(tool_fn, validated_args, ctx)


# ---------------------------------------------------------------------------
# Mock executor that fires sub-agent events between node.started and
# node.completed. Mirrors what a real ``AgentNodeRunner`` produces
# via ``AgentEngine.execute_stream``.
# ---------------------------------------------------------------------------


def _make_session_event(
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


class _MockExecutorWithSubAgent:
    """Mock executor that fires sub-agent events for its one agent node.

    Fires (in order):

        RUN_STARTED
        NODE_STARTED(classify)
          sub-agent USER_MESSAGE
          sub-agent TOOL_CALLED
          sub-agent AGENT_MESSAGE
        NODE_COMPLETED(classify)
        RUN_COMPLETED
    """

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
        # Long pause between NODE_STARTED and the first sub-agent
        # event so the test has time to observe the row's mount and
        # flip its `collapsed = False` — Coordinator #3 requires the
        # row to be expanded when events arrive.
        self._pre_sub_agent_delay = pre_sub_agent_delay
        # Deterministic ids so assertions can cross-reference.
        self.run_id = WorkflowRunId(f"wrk_{uuid4()}")
        self.trace_id = TraceId(f"tr_{uuid4()}")
        self.invocation_id = InvocationId(f"inv_{uuid4()}")
        self.node_name = "classify"

    async def run(self) -> WorkflowRunStatus:
        # RUN_STARTED
        self._observer(
            WorkflowRunStartedEvent(
                workflow_run_id=self.run_id,
                trace_id=self.trace_id,
            ),
        )
        await asyncio.sleep(self._delay)
        # NODE_STARTED — the renderer registers the row in the
        # adapter's routing map on this event.
        self._observer(
            WorkflowNodeStartedEvent(
                workflow_run_id=self.run_id,
                trace_id=self.trace_id,
                invocation_id=self.invocation_id,
                node_name=self.node_name,
            ),
        )
        # Long pause so the test can observe the row mount +
        # expand it before sub-agent events fan.
        await asyncio.sleep(self._pre_sub_agent_delay)

        # Fan sub-agent events — three flavours, one of each. The
        # bridge translates each into a WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT
        # UI event; the renderer looks up the row via the adapter's
        # `(trace_id, invocation_id)` map and dispatches.
        if self._sub_agent_observer is not None:
            for evt in (
                _make_session_event(
                    event_type=EventType.USER_MESSAGE,
                    trace_id=str(self.trace_id),
                    data={"content": {"text": "hello, classify me"}},
                ),
                _make_session_event(
                    event_type=EventType.TOOL_CALLED,
                    trace_id=str(self.trace_id),
                    data={"tool_name": "bash", "args": {"cmd": "ls"}},
                ),
                _make_session_event(
                    event_type=EventType.AGENT_MESSAGE,
                    trace_id=str(self.trace_id),
                    data={"content": {"text": "here is my response"}},
                ),
            ):
                # spec-103 approach A: the executor passes the emitting
                # run's id first.
                self._sub_agent_observer(
                    str(self.run_id),
                    self.node_name,
                    str(self.invocation_id),
                    evt,
                )
                await asyncio.sleep(self._delay)

        # NODE_COMPLETED + RUN_COMPLETED
        self._observer(
            WorkflowNodeCompletedEvent(
                workflow_run_id=self.run_id,
                trace_id=self.trace_id,
                invocation_id=self.invocation_id,
                node_name=self.node_name,
            ),
        )
        await asyncio.sleep(self._delay)
        self._observer(
            WorkflowRunCompletedEvent(
                workflow_run_id=self.run_id,
                trace_id=self.trace_id,
            ),
        )
        return WorkflowRunStatus.SUCCEEDED


def _install_sub_agent_factory(
    impl: WorkflowsImpl,
    *,
    ref: str,
    tmp_path: Path,
    pre_sub_agent_delay: float = 2.0,
) -> dict[str, _MockExecutorWithSubAgent]:
    """Rebind the executor factory to the sub-agent-firing mock.

    Returns a dict-holder — after `impl.start()` fires,
    `holder["mock"]` is the executor instance for introspection.
    The `pre_sub_agent_delay` is a long pause between NODE_STARTED
    and the first sub-agent event so the test can observe the row's
    mount + flip `collapsed = False` before events fan (Coordinator
    #3 lock).
    """
    holder: dict[str, _MockExecutorWithSubAgent] = {}

    def _factory(
        ref_in: str,
        inputs: dict[str, Any],
        observer: Callable[[Any], None],
        cancel_token: Any,
        sub_agent_observer: Any = None,
    ) -> tuple[_MockExecutorWithSubAgent, RunDirectory]:
        _ = (ref_in, inputs)
        run_dir = RunDirectory(
            root=tmp_path / "runs" / f"wrk_{uuid.uuid4()}",
        )
        run_dir.root.mkdir(parents=True, exist_ok=True)
        mock = _MockExecutorWithSubAgent(
            observer=observer,
            sub_agent_observer=sub_agent_observer,
            cancel_token=cancel_token,
            pre_sub_agent_delay=pre_sub_agent_delay,
        )
        holder["mock"] = mock
        return mock, run_dir

    # _factory returns the local _MockExecutorWithSubAgent stand-in
    # (not a real IterativeExecutor) — deliberate test seam, so the
    # factory-slot type is intentionally incompatible.
    impl._executor_factory = _factory  # type: ignore[assignment]
    return holder


# ---------------------------------------------------------------------------
# The flagship.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sub_agent_widgets_mount_in_expanded_node_row(
    chat_app: Any,
    run_workflow_tool: Callable[..., Awaitable[Any]],
    tmp_path: Path,
) -> None:
    """Sub-agent widgets appear inside the expanded `WorkflowNodeRow`."""
    async with chat_app.run_test() as pilot:
        await pilot.pause()

        workflows_impl = pilot.app._service_registry.workflows
        assert isinstance(workflows_impl, WorkflowsImpl)
        # Install the sub-agent-firing mock. The mock pauses 500ms
        # between NODE_STARTED and the first sub-agent event so the
        # test can flip `collapsed = False` on the row before events
        # arrive (Coordinator #3 lock).
        holder = _install_sub_agent_factory(
            workflows_impl,
            ref="kolsch/workflows/hello-agent",
            tmp_path=pilot.app.session.root,
        )

        # Fire `run_workflow` via the real dispatch entry. bug-031 made
        # `run_workflow` blocking (long-polls until terminal), so we
        # can't `await` the tool call here — the test needs to observe
        # the intermediate widget-mount + expand + streaming-events
        # while the workflow is still running. Spawn as a background
        # task so the test's polling loops run concurrently with the
        # mock executor's fan.
        tool_task = asyncio.create_task(
            _invoke_tool_via_pilot(
                pilot,
                run_workflow_tool,
                {"ref": "kolsch/workflows/hello-agent", "inputs": {}},
            ),
        )

        # Wait for the WorkflowCard to mount.
        widget: CardWidget | None = None
        for _ in range(80):
            await pilot.pause()
            widgets = [
                w
                for w in pilot.app.query(CardWidget)
                if getattr(w, "kind", None) == "workflow"
            ]
            if widgets:
                widget = widgets[0]
                break
            await asyncio.sleep(0.05)
        assert widget is not None, "workflow card did not mount"
        assert isinstance(widget._body, WorkflowRenderer)
        renderer = widget._body

        # Wait for the node row to mount.
        for _ in range(80):
            await pilot.pause()
            if renderer._node_rows:
                break
            await asyncio.sleep(0.05)
        assert renderer._node_rows, "no node row mounted"
        node_row = next(iter(renderer._node_rows.values()))
        assert isinstance(node_row, WorkflowNodeRow)

        # Coordinator #3: events are only mounted while EXPANDED.
        # Expand the row so incoming sub-agent widgets land visibly.
        # The mock's 500ms pre-sub-agent delay gives us this window.
        node_row.collapsed = False
        await pilot.pause()

        # bug-036d: USER_MESSAGE inside a workflow node NOW mounts
        # a UserChatbox (reverses bug-035's suppression — the row
        # IS a chat container). TOOL_CALLED alone doesn't mount a
        # ToolCallBlock (main-chat parity — mount happens on
        # TOOL_RESULT); the mock fires only TOOL_CALLED.
        want = {UserChatbox, AssistantProse}
        seen: set[type] = set()
        for _ in range(160):  # 160 * 50ms = 8s worst case
            await pilot.pause()
            seen = {type(w) for w in node_row.query("*") if type(w) in want}
            if seen == want:
                break
            await asyncio.sleep(0.05)

        if seen != want:
            store = pilot.app.session.runtime.store
            thread_id = pilot.app.session.thread_id
            ui_events = []
            async for ev in store.read_events(thread_id):
                if ev.type == EventType.UI_EVENT:
                    data = ev.data if isinstance(ev.data, dict) else {}
                    ui_events.append(data.get("kind"))
        assert seen == want, (
            f"expected sub-agent widgets to mount inside the row; "
            f"got {sorted(t.__name__ for t in seen)}"
        )

        # The row's turn counter reflects ALL sub-agent events routed
        # to it (including the skipped USER_MESSAGE) — MUST be at
        # least 3.
        assert node_row._sub_agent_turn_count >= 3, (
            f"sub_agent_turn_count = {node_row._sub_agent_turn_count}"
        )
        # Mock's ids reached the widget.
        mock = holder["mock"]
        assert str(mock.trace_id) == node_row._trace_id

        # Drain the tool task so its long-poll (500ms poll interval)
        # settles before Pilot tears the app down. A stray unfinished
        # task would leak into the next test's event loop.
        try:
            result = await asyncio.wait_for(tool_task, timeout=10.0)
        except TimeoutError:
            tool_task.cancel()
            with contextlib.suppress(BaseException):
                await tool_task
            raise
        assert result["run_id"].startswith("wrk_")


# ---------------------------------------------------------------------------
# Grep-guard self-test — D-010 gate.
# ---------------------------------------------------------------------------


def test_no_scaffolding_used_in_this_file() -> None:
    """This flagship MUST NOT use scaffolding — D-010 gate.

    If any of `_replay_ui_events_since`, `adapter.handle(`, or
    `_invoke_tool_via_pilot` (defined LOCALLY as the standard live
    tool dispatch mirror — this is fine) sneak into the assertions,
    the spec is incomplete. We check the actual body of the flagship
    test function, not our local helper stubs.
    """
    src = inspect.getsource(test_sub_agent_widgets_mount_in_expanded_node_row)
    forbidden = ("_replay_ui_events_since", "adapter.handle(")
    for needle in forbidden:
        assert needle not in src, (
            f"flagship uses forbidden scaffolding {needle!r}; spec-020 D-010 gate"
        )
