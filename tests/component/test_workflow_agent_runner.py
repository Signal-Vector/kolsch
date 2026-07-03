"""Component tests for Phase 3.2 Step 1+2 — :class:`AgentRunner`.

Exercises the assembly + engine-invocation path in isolation. Step 1
shipped the SKELETON behind a stubbed engine seam; Step 2 wired the
real :class:`engine.AgentEngine` injection, the
:class:`engine.ExecutionContext` construction (carrying ``trace_id``
+ extended caller chain), and the
:class:`engine.AgentExecutionError` → :class:`WorkflowNodeFailed`
remap. These tests inject a tiny fake engine that captures the call
shape so the runner-side construction is verifiable without standing
up the full :class:`AgentEngine` fixture (that lives at the Step 4
end-to-end layer).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from assets.catalog import AssetCatalog, AssetEntry
from engine.context import AgentExecutionError
from schemas import (
    Actor,
    AgentId,
    Event,
    EventId,
    EventType,
    FromWorkflowRun,
    IOSpec,
    ModelSpec,
    ResolvedAgentConfig,
    SessionId,
    ThreadId,
)
from store import SqliteBackend
from store.session_store import SessionStore
from workflows.builtins.agent import AgentRunner
from workflows.context import NodeContext
from workflows.errors import AgentRefUnresolved, WorkflowNodeFailed
from workflows.ids import InvocationId, TraceId, WorkflowRunId
from workflows.parser import AgentNodeDef

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from engine import AgentEngine, ExecutionContext


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


_AGENT_REF = "demo/agents/classifier"


class _FakeEngine:
    """Minimal :class:`AgentEngine` stand-in.

    The runner consumes only ``execute(config, context, *, inputs)``;
    fakes can record the call shape without standing up the full
    engine wiring. Tests that need to exercise real engine integration
    end-to-end live in Step 4's ``tests/component/test_workflow_agent_runner_e2e.py``.
    """

    def __init__(self, *, response_content: dict[str, Any] | None = None) -> None:
        self.response_content = response_content if response_content is not None else {}
        self.calls: list[
            tuple[ResolvedAgentConfig, ExecutionContext, dict[str, Any]]
        ] = []
        self.raise_on_next: BaseException | None = None

    async def execute(
        self,
        config: ResolvedAgentConfig,
        context: ExecutionContext,
        *,
        inputs: dict[str, Any] | None = None,
    ) -> Event:
        self.calls.append((config, context, dict(inputs or {})))
        if self.raise_on_next is not None:
            exc = self.raise_on_next
            self.raise_on_next = None
            raise exc
        return _make_agent_message_event(
            session_id=SessionId(f"ses_{uuid4()}"),
            thread_id=context.thread_id,
            content=self.response_content,
        )


def _as_engine(fake: _FakeEngine) -> AgentEngine:
    """Cast the fake to satisfy the runner's annotation. The runner
    only invokes ``execute(...)`` — full structural conformance
    isn't needed for these unit tests."""
    return fake


def _make_resolved_config(
    *,
    mode: str = "single_shot",
) -> ResolvedAgentConfig:
    return ResolvedAgentConfig(
        agent_id=AgentId("ag_demo_classifier"),
        name="classifier",
        description="A test classifier agent.",
        model=ModelSpec(
            provider="fake",
            model="fake-model",
            parameters={"temperature": 0.42},
        ),
        prompt="You are a classifier.",
        io=IOSpec(
            mode=mode,
            inputs={"text": {"type": "str"}},
            outputs={"label": {"type": "str"}},
        ),
    )


def _make_catalog(*, with_agent: bool = True) -> AssetCatalog:
    assets: dict[str, AssetEntry] = {}
    if with_agent:
        assets[_AGENT_REF] = AssetEntry(
            ref=_AGENT_REF,
            asset_type="agent",
            bundle="demo",
            path="/dev/null",  # unused — config_builder is the loader seam
        )
    return AssetCatalog(assets=assets)


def _make_agent_message_event(
    *,
    session_id: SessionId,
    thread_id: ThreadId,
    content: dict[str, Any],
) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=thread_id,
        session_id=session_id,
        type=EventType.AGENT_MESSAGE,
        data={
            "content": content,
            "exchange_id": EventId(f"ev_{uuid4()}"),
        },
        actor=Actor(kind="agent", id="ag_demo_classifier"),
        created_at=datetime.now(UTC),
    )


def _make_failed_event(*, session_id: SessionId, thread_id: ThreadId) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=thread_id,
        session_id=session_id,
        type=EventType.THREAD_EXCHANGE_FAILED,
        data={"reason": "fake-failure"},
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


def _make_ctx(
    *,
    workspace: Path,
    parent_event_id: EventId | None = None,
    trace_id: TraceId | None = None,
    caller_chain: tuple[str, ...] = (),
) -> NodeContext:
    ctx = NodeContext.for_test(
        workflow_run_id=str(WorkflowRunId(f"wrk_{uuid4()}")),
        invocation_id=str(InvocationId(f"inv_{uuid4()}")),
        bundle="kolsch",
        workspace_root=workspace,
    )
    object.__setattr__(
        ctx,
        "parent_event_id",
        parent_event_id or EventId(f"ev_{uuid4()}"),
    )
    if trace_id is not None:
        object.__setattr__(ctx, "trace_id", trace_id)
    if caller_chain:
        object.__setattr__(ctx, "caller_chain", caller_chain)
    return ctx


def _make_node_def(*, outputs: IOSpec | None = None) -> AgentNodeDef:
    return AgentNodeDef(type="kolsch/nodes/agent", agent=_AGENT_REF, outputs=outputs)


@pytest.fixture
async def session_store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    try:
        yield SessionStore(root=tmp_path, backend=backend)
    finally:
        await backend.close()


def _make_runner(
    *,
    catalog: AssetCatalog,
    session_store: SessionStore,
    config: ResolvedAgentConfig | None = None,
    engine: _FakeEngine | None = None,
) -> tuple[AgentRunner, _FakeEngine]:
    cfg = config or _make_resolved_config()

    async def _builder(_ref: str) -> ResolvedAgentConfig:
        return cfg

    eng = engine or _FakeEngine()
    runner = AgentRunner(
        catalog=catalog,
        session_store=session_store,
        config_builder=_builder,
        engine=_as_engine(eng),
    )
    return runner, eng


# ---------------------------------------------------------------------------
# Step 1 — assembly path tests
# ---------------------------------------------------------------------------


async def test_assembly_happy_path(
    tmp_path: Path,
    session_store: SessionStore,
) -> None:
    """End-to-end assembly: catalog → builder → session → engine
    seam → output extraction."""
    parent_event_id = EventId(f"ev_{uuid4()}")
    runner, engine = _make_runner(
        catalog=_make_catalog(),
        session_store=session_store,
        engine=_FakeEngine(response_content={"label": "positive"}),
    )
    ctx = _make_ctx(workspace=tmp_path, parent_event_id=parent_event_id)

    out = await runner.run(
        node_name="classify",
        node_def=_make_node_def(),
        bound_inputs={"text": "hello"},
        state={},
        ctx=ctx,
    )

    # The child session was created carrying the FromWorkflowRun pointer.
    assert ctx.spawned_session_id is not None
    rebuilt = await session_store.get_session(ctx.spawned_session_id)
    expected_pointer = FromWorkflowRun(
        workflow_run_id=WorkflowRunId(ctx.workflow_run_id),
        invocation_id=InvocationId(ctx.invocation_id),
        event_id=parent_event_id,
    )
    assert rebuilt.from_workflow_run == expected_pointer
    assert rebuilt.name == f"{ctx.workflow_run_id}::classify"

    # The engine saw the (config, context, inputs) tuple correctly.
    assert len(engine.calls) == 1
    _config, exec_ctx, inputs = engine.calls[0]
    assert inputs == {"text": "hello"}
    assert exec_ctx.trigger == {"kind": "invocation", "inputs": {"text": "hello"}}

    # The captured output came from the agent.message event.
    assert out == {"label": "positive"}


async def test_outputs_override_replaces_only_outputs(
    tmp_path: Path,
    session_store: SessionStore,
) -> None:
    """AC 1.3 — ``outputs:`` override replaces ``config.io.outputs``;
    ``config.io.inputs`` and identity fields are untouched."""
    base = _make_resolved_config()
    runner, engine = _make_runner(
        catalog=_make_catalog(),
        session_store=session_store,
        config=base,
        engine=_FakeEngine(response_content={"classification": "spam"}),
    )
    override = IOSpec(
        mode="single_shot",
        inputs={},
        outputs={"classification": {"type": "str"}},
    )
    ctx = _make_ctx(workspace=tmp_path)

    out = await runner.run(
        node_name="classify",
        node_def=_make_node_def(outputs=override),
        bound_inputs={"text": "hello"},
        state={},
        ctx=ctx,
    )

    assert out == {"classification": "spam"}
    passed_config, _ctx, _inputs = engine.calls[0]
    assert passed_config.io.outputs == {"classification": {"type": "str"}}
    # Inputs preserved from the agent's declared shape; only the
    # ``outputs`` slice is patched.
    assert passed_config.io.inputs == base.io.inputs
    assert passed_config.io.mode == base.io.mode


async def test_identity_fields_untouched_by_runner(
    tmp_path: Path,
    session_store: SessionStore,
) -> None:
    """AC 1.3 — identity overrides forbidden (master spec §4.1.2.4).

    The runner MUST NOT rewrite ``agent_id``, ``model``, ``prompt``,
    or any field outside ``io.outputs``.
    """
    base = _make_resolved_config()
    runner, engine = _make_runner(
        catalog=_make_catalog(),
        session_store=session_store,
        config=base,
    )
    override = IOSpec(
        mode="single_shot",
        inputs={},
        outputs={"label": {"type": "str"}},
    )
    ctx = _make_ctx(workspace=tmp_path)

    await runner.run(
        node_name="classify",
        node_def=_make_node_def(outputs=override),
        bound_inputs={},
        state={},
        ctx=ctx,
    )
    passed_config = engine.calls[0][0]
    assert passed_config.agent_id == base.agent_id
    assert passed_config.name == base.name
    assert passed_config.description == base.description
    assert passed_config.prompt == base.prompt
    assert passed_config.model == base.model
    assert passed_config.runtime == base.runtime
    assert passed_config.subsystems == base.subsystems


async def test_unknown_ref_raises_workflow_node_failed(
    tmp_path: Path,
    session_store: SessionStore,
) -> None:
    """AC 1.6 — defensive guard for validator drift: missing catalog
    entry surfaces as ``WorkflowNodeFailed`` wrapping
    ``AgentRefUnresolved``. Builder + engine seams must NOT fire."""
    runner, engine = _make_runner(
        catalog=_make_catalog(with_agent=False),
        session_store=session_store,
    )
    ctx = _make_ctx(workspace=tmp_path)

    with pytest.raises(WorkflowNodeFailed) as exc_info:
        await runner.run(
            node_name="classify",
            node_def=_make_node_def(),
            bound_inputs={},
            state={},
            ctx=ctx,
        )
    assert isinstance(exc_info.value.cause, AgentRefUnresolved)
    assert exc_info.value.cause.ref == _AGENT_REF
    assert len(engine.calls) == 0
    assert ctx.spawned_session_id is None


# ---------------------------------------------------------------------------
# Step 2 — engine wiring + error remap
# ---------------------------------------------------------------------------


async def test_trace_id_propagates_to_execution_context(
    tmp_path: Path,
    session_store: SessionStore,
) -> None:
    """AC 2.2 — ``ExecutionContext.trace_id == ctx.trace_id``.

    A fresh trace_id here would be a regression: the spawned session's
    events would be uncorrelatable with the workflow run.
    """
    trace_id = TraceId(f"tr_{uuid4()}")
    runner, engine = _make_runner(
        catalog=_make_catalog(),
        session_store=session_store,
    )
    ctx = _make_ctx(workspace=tmp_path, trace_id=trace_id)

    await runner.run(
        node_name="classify",
        node_def=_make_node_def(),
        bound_inputs={},
        state={},
        ctx=ctx,
    )

    _config, exec_ctx, _inputs = engine.calls[0]
    assert exec_ctx.trace_id == trace_id


async def test_caller_chain_extension_proximate_first(
    tmp_path: Path,
    session_store: SessionStore,
) -> None:
    """AC 2.3 — caller chain prepends the agent ref (proximate first).

    Reversed order would break authz audit forensics.
    """
    runner, engine = _make_runner(
        catalog=_make_catalog(),
        session_store=session_store,
    )
    parent_chain = ("kolsch/workflows/parent", "kolsch/workflows/grandparent")
    ctx = _make_ctx(workspace=tmp_path, caller_chain=parent_chain)

    await runner.run(
        node_name="classify",
        node_def=_make_node_def(),
        bound_inputs={},
        state={},
        ctx=ctx,
    )

    _config, exec_ctx, _inputs = engine.calls[0]
    assert exec_ctx.caller_chain == (_AGENT_REF, *parent_chain)


async def test_agent_execution_error_remaps_to_workflow_node_failed(
    tmp_path: Path,
    session_store: SessionStore,
) -> None:
    """AC 2.4 — :class:`AgentExecutionError` becomes
    :class:`WorkflowNodeFailed(cause=)`; ``__cause__`` preserved so the
    executor's failure path can read the chained exception."""
    engine = _FakeEngine()
    failed_event = _make_failed_event(
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
    )
    engine.raise_on_next = AgentExecutionError(failed_event)
    runner, _ = _make_runner(
        catalog=_make_catalog(),
        session_store=session_store,
        engine=engine,
    )
    ctx = _make_ctx(workspace=tmp_path)

    with pytest.raises(WorkflowNodeFailed) as exc_info:
        await runner.run(
            node_name="classify",
            node_def=_make_node_def(),
            bound_inputs={},
            state={},
            ctx=ctx,
        )

    raised = exc_info.value
    assert isinstance(raised.cause, AgentExecutionError)
    # The spawned session was created BEFORE the engine raised, so the
    # ctx still carries its id — useful for the executor's payload
    # builder (Step 3) which records ``spawned_session_id`` on
    # ``workflow.node.failed`` payloads in a future tightening.
    assert ctx.spawned_session_id is not None
    # ``raise ... from exc`` preserves ``__cause__`` for the canonical
    # error envelope.
    assert raised.__cause__ is raised.cause


async def test_conversational_mode_uses_user_message_trigger(
    tmp_path: Path,
    session_store: SessionStore,
) -> None:
    """Trigger shape follows the agent's declared mode — a workflow
    targeting a conversational agent posts a ``user_message`` trigger
    so the engine's preflight doesn't reject."""
    runner, engine = _make_runner(
        catalog=_make_catalog(),
        session_store=session_store,
        config=_make_resolved_config(mode="conversational"),
    )
    ctx = _make_ctx(workspace=tmp_path)

    await runner.run(
        node_name="classify",
        node_def=_make_node_def(),
        bound_inputs={"text": "hello"},
        state={},
        ctx=ctx,
    )

    _config, exec_ctx, _inputs = engine.calls[0]
    assert exec_ctx.trigger == {
        "kind": "user_message",
        "content": {"text": "hello"},
    }
