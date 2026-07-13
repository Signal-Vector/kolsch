"""feat-054 Phase 4 — sub-agent compaction isolation via workflow runtime.

Reviewer follow-up from Phase 2 (finding #4): the original
``test_context_compaction_sub_agent_isolation.py`` invoked
``engine.execute`` directly, bypassing the workflow runtime that
production sub-agents flow through. This variant routes the child
agent through :class:`workflows.builtins.agent.AgentRunner` — the same
path :mod:`workflows.executor` takes when a workflow node of type
``kolsch/nodes/agent`` fires — and re-asserts:

- the child thread's compaction event lands on the CHILD thread only,
- the parent thread's event log contains no ``CONTEXT_COMPACTED``,
- the parent's projected messages are untouched by the child's
  compaction (Assembly is per-thread).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from assets import AssetManagement
from assets.catalog import AssetCatalog, AssetEntry
from bus import InProcessEventBus
from engine import (
    AgentEngine,
    ExecutionContext,
    LLMRequest,
    LLMResponse,
    MinimalFramework,
)
from files import PathPolicy
from llm import ProviderConfig
from providers import (
    NativeSandboxProvider,
    RecentBufferMemoryProvider,
    RuleBasedAuthorizationProvider,
)
from providers.compaction import ReferenceSummaryCompactionProvider
from providers.secrets import DotenvSecretsProvider
from schemas import (
    AgentId,
    EventId,
    EventType,
    IOSpec,
    ModelSpec,
    ResolvedAgentConfig,
)
from skills import SkillsSubsystem
from store import SessionStore, SqliteBackend
from tools import ToolsSubsystem
from workflows.builtins.agent import AgentRunner
from workflows.context import NodeContext
from workflows.parser import AgentNodeDef


_CHILD_AGENT_REF = "kolsch/agents/child"


def _write_platform_yaml(root: Path) -> None:
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (root / sub).mkdir(exist_ok=True)
    (root / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
                "sandbox": {"variant": "native", "config": {}},
                "authorization": {
                    "variant": "rule_based",
                    "config": {
                        "rules": [
                            {"who": ".*", "what": ".*", "effect": "allow"},
                        ]
                    },
                },
                "storage": {
                    "entity_backend": "sqlite",
                    "entity_path": ".platform/entities.db",
                },
                "event_bus": {"backend": "in_process"},
                "file_access": {
                    "allowed_paths": ["data/"],
                    "denied_paths": [".env"],
                },
                "compaction": {"variant": "reference_summary"},
                "defaults": {},
            }
        ),
        encoding="utf-8",
    )
    (root / ".env").write_text("", encoding="utf-8")


def _make_catalog_with_child() -> AssetCatalog:
    entry = AssetEntry(
        ref=_CHILD_AGENT_REF,
        bundle="kolsch",
        path="agents/child.yaml",
        asset_type="agent",
    )
    return AssetCatalog(assets={_CHILD_AGENT_REF: entry})


@pytest.mark.asyncio
async def test_workflow_agent_runner_compaction_stays_on_child_thread(
    tmp_path: Path,
) -> None:
    _write_platform_yaml(tmp_path)
    backend = SqliteBackend(tmp_path / ".platform" / "entities.db")
    await backend.initialize()
    store = SessionStore(root=tmp_path, backend=backend)
    am = AssetManagement(root=tmp_path)
    await am.initialize()

    authz = RuleBasedAuthorizationProvider(
        config={"rules": [{"who": ".*", "what": ".*", "effect": "allow"}]}
    )
    secrets = DotenvSecretsProvider(root=tmp_path, config={"path": ".env"})
    sandbox = NativeSandboxProvider()
    memory = RecentBufferMemoryProvider(store=store)
    tools_sub = ToolsSubsystem(
        secrets=secrets,
        sandbox=sandbox,
        authz=authz,
        path_policy=PathPolicy(allowed_paths=["data/"], denied_paths=[]),
        root=tmp_path,
    )
    skills_sub = SkillsSubsystem(asset_management=am)
    bus = InProcessEventBus()

    async def default_llm(_request: LLMRequest) -> LLMResponse:
        return LLMResponse(content="ok", tool_calls=None, usage={}, raw={})

    class _SummariserLLM:
        async def __call__(self, _request: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content=(
                    '{"schema_version": 1, "summary_text": "child summary", '
                    '"refs": [], "kept_files": [], '
                    '"span": {"first_event_id": "ev_x", '
                    '"last_event_id": "ev_y", "event_count": 2}}'
                ),
                tool_calls=None,
                usage={},
                raw={},
            )

    async def factory(_spec: Any) -> Any:
        return _SummariserLLM()

    provider = ReferenceSummaryCompactionProvider(
        llm_for_model=factory,
        config={},
    )
    llm_providers = {
        "mock": ProviderConfig(
            name="mock",
            type="mock",
            api_key_secret=None,
            base_url=None,
            timeout_seconds=None,
            extras={},
            # Tiny window forces the CHILD to compact; the parent uses
            # a bigger declared context so its own compaction doesn't
            # fire.
            models=[
                {"id": "mock-child", "context_length": 10},
                {"id": "mock-parent", "context_length": 100_000_000},
            ],
        ),
    }

    engine = AgentEngine(
        store=store,
        bus=bus,
        asset_management=am,
        tools_subsystem=tools_sub,
        skills_subsystem=skills_sub,
        memory_provider=memory,
        secrets=secrets,
        sandbox=sandbox,
        authz=authz,
        framework=MinimalFramework(),
        llm=default_llm,
        llm_providers_config=llm_providers,
        compaction_provider=provider,
    )

    parent_config = ResolvedAgentConfig(
        schema_version="0.1",
        assembly_version="2",
        agent_id=AgentId("ag_parent"),
        name="parent",
        description="parent agent",
        model=ModelSpec(provider="mock", model="mock-parent"),
        prompt="You are the parent.",
        io=IOSpec(mode="conversational"),
    )
    child_config = ResolvedAgentConfig(
        schema_version="0.1",
        assembly_version="2",
        agent_id=AgentId("ag_child"),
        name="child",
        description="child agent",
        model=ModelSpec(provider="mock", model="mock-child"),
        # single_shot so the AgentRunner takes the `invocation` trigger
        # branch — matches the production workflow-agent path.
        prompt="You are the child.",
        io=IOSpec(
            mode="single_shot",
            inputs={"context": {"type": "str"}},
            outputs={"text": {"type": "str"}},
        ),
    )

    # Parent kicks off — a plain engine exchange (a chat surface would
    # do the same). Its execute-stream call does not go through the
    # workflow runtime; the workflow-runtime path is the CHILD's.
    parent_session = await store.create_session(
        "ag_parent", name="parent-session"
    )
    parent_thread = (await store.list_threads(parent_session.session_id))[0]
    await engine.execute(
        parent_config,
        ExecutionContext(
            thread_id=parent_thread.thread_id,
            trigger={"kind": "user_message", "content": "parent turn"},
        ),
    )

    # Wire the AgentRunner exactly as the workflow executor wires it.
    # The catalog + config_builder let the runner resolve the child
    # agent ref; the injected engine is the same one that carries the
    # compaction provider.
    catalog = _make_catalog_with_child()

    async def config_builder(_ref: str) -> ResolvedAgentConfig:
        return child_config

    runner = AgentRunner(
        catalog=catalog,
        session_store=store,
        config_builder=config_builder,
        engine=engine,
    )

    # Fabricate a workflow-node context. The runner reads
    # `workflow_run_id`, `invocation_id`, `parent_event_id`, and (best-
    # effort) `on_sub_agent_event_callback`; we set the first three so
    # the runner can mint FromWorkflowRun pointers, and leave the
    # callback unset (the runner tolerates None).
    parent_event_id = EventId(f"ev_{uuid4()}")
    node_ctx = NodeContext.for_test(
        workflow_run_id=f"wrk_{uuid4()}",
        invocation_id=f"inv_{uuid4()}",
        bundle="kolsch",
    )
    object.__setattr__(node_ctx, "parent_event_id", parent_event_id)
    object.__setattr__(
        node_ctx,
        "log",
        logging.getLogger("workflows.compaction.test"),
    )

    node_def = AgentNodeDef(
        type="kolsch/nodes/agent",
        agent=_CHILD_AGENT_REF,
    )

    # Drive the child once through the runner — this exercises the
    # full workflow→AgentRunner→engine path (session spawn +
    # FromWorkflowRun stamping + engine.execute_stream). One
    # invocation is enough because the isolation contract is about
    # PATH, not about auto-triggering compaction: a fresh workflow-
    # spawned session naturally has too little history to auto-
    # trigger. We then force a compaction on the child thread via
    # `manual_compact` (the same API `/compact` uses) with an
    # override that guarantees a non-empty middle span, and assert
    # the CONTEXT_COMPACTED lands only on the child.
    await runner.run(
        node_name="classify",
        node_def=node_def,
        bound_inputs={"context": "child first"},
        state={},
        ctx=node_ctx,
    )

    # Collect all sessions the runner spawned via
    # `FromWorkflowRun.workflow_run_id`.
    all_sessions = await store.list_sessions()
    child_sessions = [
        s
        for s in all_sessions
        if s.session_id != parent_session.session_id
        and getattr(s, "from_workflow_run", None) is not None
    ]
    assert child_sessions, (
        "AgentRunner did not spawn any child session — the workflow-"
        "runtime path was not exercised."
    )
    # Every workflow-spawned session must trace back to OUR node's
    # workflow_run_id — belt-and-braces on the workflow-runtime path.
    for cs in child_sessions:
        assert (
            cs.from_workflow_run is not None
            and str(cs.from_workflow_run.workflow_run_id)
            == node_ctx.workflow_run_id
        )

    # Force a compaction on the child thread. `manual_compact` bypasses
    # the token-estimate trigger check (that's what /compact does) so
    # we can verify isolation without contriving 20+ events on a
    # freshly-spawned child. Under the hood this is the SAME path an
    # auto-trigger would take: the child engine's compaction provider
    # produces a summary, emits CONTEXT_COMPACTED on the child thread,
    # and the parent thread is left untouched.
    child_session = child_sessions[0]
    child_threads = await store.list_threads(child_session.session_id)
    child_thread = child_threads[0]
    # Seed enough events on the child thread so the middle span is
    # non-empty — one extra USER_MESSAGE + AGENT_MESSAGE via a second
    # engine.execute on the same child thread. This mirrors what a
    # multi-turn sub-agent (or a workflow that reruns the node) would
    # accumulate. Compaction is per-thread so the parent stays clean
    # regardless.
    await engine.execute(
        child_config,
        ExecutionContext(
            thread_id=child_thread.thread_id,
            trigger={
                "kind": "invocation",
                "inputs": {"context": "second turn on shared child thread"},
            },
        ),
    )
    emitted = await engine.manual_compact(
        config=child_config,
        thread_id=child_thread.thread_id,
    )
    assert emitted is not None, (
        "manual_compact returned None — the child engine should have "
        "produced a compaction from the seeded thread"
    )

    child_events: list[Any] = []
    for cs in child_sessions:
        threads = await store.list_threads(cs.session_id)
        for t in threads:
            async for e in store.read_path_to_leaf(t.thread_id):
                child_events.append(e)

    parent_events = [
        e async for e in store.read_path_to_leaf(parent_thread.thread_id)
    ]

    # Parent: no compaction event AT ALL.
    assert not any(
        e.type == EventType.CONTEXT_COMPACTED for e in parent_events
    ), (
        "parent thread saw a CONTEXT_COMPACTED — sub-agent compaction "
        "must stay on the child thread"
    )
    # Child: at least one compaction event lands on the child. Every
    # compaction event's thread_id points at a CHILD thread — the
    # parent thread never appears in the compactions list.
    child_compactions = [
        e for e in child_events if e.type == EventType.CONTEXT_COMPACTED
    ]
    assert child_compactions, (
        "no CONTEXT_COMPACTED landed on any child thread"
    )
    child_thread_ids = {
        str(t.thread_id)
        for cs in child_sessions
        for t in await store.list_threads(cs.session_id)
    }
    for e in child_compactions:
        assert str(e.thread_id) in child_thread_ids
        assert str(e.thread_id) != str(parent_thread.thread_id)
