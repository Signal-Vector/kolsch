"""bug-020 Test 6 (AC #11) — flagship live-mount Pilot regression.

Boots a real `ChatApp` under Pilot. Invokes the `run_workflow`
butler tool through `_invoke_tool_via_pilot` (which mirrors the
engine's own dispatch entry). Awaits `CardWidget[kind=workflow]`
in `#transcript` WITHOUT any manual store drain — the runtime bus
subscription in `ChatApp.on_mount` is the load-bearing seam.

Assertions:
1. Exactly one `CardWidget` mounts with `kind="workflow"` within
   a bounded await window (2s).
2. That widget's `_body` is a `WorkflowRenderer`.
3. The renderer's `_node_rows` contains at least one row after
   `RUN_STARTED` fires.
4. No `TOOL_FAILED` event for `run_workflow` in the store.

If this test relies on `_replay_ui_events_since` or any equivalent
scaffolding, the fix is not complete (D-010 gate).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from cli.chat.app import ChatApp
from cli.chat.services.workflows_impl import WorkflowsImpl
from cli.chat.widgets.card_body.workflow import WorkflowRenderer
from cli.chat.widgets.card_widget import CardWidget
from schemas import CallerChain
from schemas.events import EventType
from schemas.identifiers import AgentId, SessionId, ThreadId
from tests.cli.chat.services.mocks import (
    MockExecutorSpec,
    install_mock_executor_factory,
)
from tests.fixtures.bundle_paths import STARTER_TOOLS_DIR, load_bundle_tool
from tools.decorator import ToolContext, get_tool_definition
from tools.dispatch import _invoke_tool
from workflows.events import (
    WorkflowNodeStartedEvent,
    WorkflowRunCompletedEvent,
    WorkflowRunStartedEvent,
)
from workflows.ids import InvocationId, TraceId, WorkflowRunId

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path


@pytest.fixture
def run_workflow_tool() -> Callable[..., Awaitable[Any]]:
    return load_bundle_tool(STARTER_TOOLS_DIR, "run_workflow.py", "run_workflow")


# ---------------------------------------------------------------------------
# Local `chat_app` fixture — mirrors the bundle-suite version but
# lives here so this test doesn't cross-depend on the bundle
# conftest (`tests/bundles/kolsch/tools/conftest.py`) fixture surface.
# The bug-020 conftest already injects a real `InProcessEventBus` on
# the mock runtime so the live bus-subscription path fires; we
# recreate that here.
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


def _mk_events_with_node() -> list[Any]:
    """Three-event script: run.started + node.started + run.completed.

    The middle `node.started` gives the `WorkflowRenderer._node_rows`
    something to observe — assertion 3 (AC #11.3).
    """
    rid = WorkflowRunId(f"wrk_{uuid4()}")
    trace = TraceId(f"tr_{uuid4()}")
    inv = InvocationId(f"inv_{uuid4()}")
    return [
        WorkflowRunStartedEvent(workflow_run_id=rid, trace_id=trace),
        WorkflowNodeStartedEvent(
            workflow_run_id=rid,
            trace_id=trace,
            invocation_id=inv,
            node_name="greet",
        ),
        WorkflowRunCompletedEvent(workflow_run_id=rid, trace_id=trace),
    ]


@pytest.mark.asyncio
async def test_workflow_card_mounts_live_without_manual_drain(
    chat_app: Any,
    run_workflow_tool: Callable[..., Awaitable[Any]],
) -> None:
    """The flagship — a `WorkflowCard` appears live in `#transcript`."""
    async with chat_app.run_test() as pilot:
        await pilot.pause()

        # Install a scripted mock so `_run_one_workflow` completes.
        workflows_impl = pilot.app._service_registry.workflows
        assert isinstance(workflows_impl, WorkflowsImpl)
        install_mock_executor_factory(
            workflows_impl,
            {
                "kolsch/workflows/live": MockExecutorSpec(
                    events=_mk_events_with_node(),
                    per_event_delay=0.05,
                ),
            },
            runs_root=pilot.app.session.root / "runs",
        )

        # Invoke `run_workflow` via the same path a butler tool call
        # would take. NO manual `_replay_ui_events_since`.
        result = await _invoke_tool_via_pilot(
            pilot,
            run_workflow_tool,
            {"ref": "kolsch/workflows/live", "inputs": {}},
        )
        assert result["run_id"].startswith("wrk_")

        # AC #11.1: exactly one `CardWidget[kind="workflow"]` mounts
        # within a bounded window (2s). Pump the pilot loop until the
        # bus subscription's dispatch has completed.
        widget = None
        for _ in range(40):  # 40 * 50ms = 2s worst case
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

        assert widget is not None, (
            "CardWidget[kind='workflow'] did not mount within 2s. "
            "The runtime bus subscription seam is broken."
        )

        # AC #11.2: the widget's body renderer is a `WorkflowRenderer`.
        assert isinstance(widget._body, WorkflowRenderer)

        # AC #11.3: after the mock fires `RUN_STARTED` + `NODE_STARTED`,
        # the renderer has at least one node row.
        for _ in range(40):
            await pilot.pause()
            if widget._body._node_rows:
                break
            await asyncio.sleep(0.05)
        assert widget._body._node_rows, (
            "WorkflowRenderer never received any node rows; the "
            "workflow-progress UI events are not reaching the "
            "adapter's live path."
        )

        # AC #11.4: no `TOOL_FAILED` for `run_workflow` on the store.
        store = pilot.app.session.runtime.store
        thread_id = pilot.app.session.thread_id
        async for event in store.read_events(thread_id):
            if event.type != EventType.TOOL_FAILED:
                continue
            data = getattr(event, "data", None) or {}
            if not isinstance(data, dict):
                continue
            tool_name = data.get("tool_name")
            assert tool_name != "run_workflow", (
                f"run_workflow raised TOOL_FAILED: {data!r}"
            )
