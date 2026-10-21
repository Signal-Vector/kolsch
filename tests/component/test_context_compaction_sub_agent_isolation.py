"""feat-054 Phase 2 — sub-agent compaction isolation (AC #12).

A sub-agent's compaction lands on the child thread ONLY; the parent
thread's projected context is unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from assets import AssetManagement
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
    EventType,
    IOSpec,
    ModelSpec,
    ResolvedAgentConfig,
)
from skills import SkillsSubsystem
from store import SessionStore, SqliteBackend
from tools import ToolsSubsystem


def _write_platform_yaml(root: Any) -> None:
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
                        "rules": [{"who": ".*", "what": ".*", "effect": "allow"}]
                    },
                },
                "storage": {
                    "entity_backend": "sqlite",
                    "entity_path": ".platform/entities.db",
                },
                "event_bus": {"backend": "in_process"},
                "file_access": {"allowed_paths": ["data/"], "denied_paths": [".env"]},
                "compaction": {"variant": "reference_summary"},
                "defaults": {},
            }
        ),
        encoding="utf-8",
    )
    (root / ".env").write_text("", encoding="utf-8")


@pytest.mark.asyncio
async def test_sub_agent_compaction_isolated_to_child_thread(tmp_path: Any) -> None:
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

    async def default_llm(request: LLMRequest) -> LLMResponse:
        return LLMResponse(content="ok", tool_calls=None, usage={}, raw={})

    class _SummariserLLM:
        async def __call__(self, request: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content=(
                    '{"schema_version": 1, "summary_text": "s", "refs": [], '
                    '"kept_files": [], '
                    '"span": {"first_event_id": "ev_x", "last_event_id": "ev_y", '
                    '"event_count": 2}}'
                ),
                tool_calls=None,
                usage={},
                raw={},
            )

    async def factory(_spec: Any, *, context: Any = None) -> Any:
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
            # Tiny window for the CHILD; parent gets a bigger window
            # so its compaction doesn't fire.
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
        prompt="You are the child.",
        io=IOSpec(mode="conversational"),
    )

    # Parent session + thread (via the store).
    parent_session = await store.create_session(
        "ag_parent",
        name="parent-session",
    )
    parent_thread = (await store.list_threads(parent_session.session_id))[0]
    parent_ctx = ExecutionContext(
        thread_id=parent_thread.thread_id,
        trigger={"kind": "user_message", "content": "parent turn"},
    )
    await engine.execute(parent_config, parent_ctx)

    # Child session + child thread — how the workflow AgentRunner
    # spawns a sub-agent (separate session, separate thread).
    child_session = await store.create_session(
        "ag_child",
        name="child-session",
    )
    child_thread = (await store.list_threads(child_session.session_id))[0]

    child_ctx1 = ExecutionContext(
        thread_id=child_thread.thread_id,
        trigger={"kind": "user_message", "content": "child first"},
    )
    await engine.execute(child_config, child_ctx1)
    child_ctx2 = ExecutionContext(
        thread_id=child_thread.thread_id,
        trigger={
            "kind": "user_message",
            "content": "child second turn adds pressure",
        },
    )
    await engine.execute(child_config, child_ctx2)

    parent_events = [e async for e in store.read_path_to_leaf(parent_thread.thread_id)]
    child_events = [e async for e in store.read_path_to_leaf(child_thread.thread_id)]

    # Parent has NO compaction event (its window is huge).
    assert not any(e.type == EventType.CONTEXT_COMPACTED for e in parent_events)
    # Child DOES have a compaction event.
    assert any(e.type == EventType.CONTEXT_COMPACTED for e in child_events)
