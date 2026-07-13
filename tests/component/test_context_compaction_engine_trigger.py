"""feat-054 Phase 2 — engine trigger + compaction pre-flight.

Covers:

- Provider ``None``: no compaction, no emit.
- Provider present + context_length ``None``: no compaction, no emit.
- Provider present + estimate above threshold: emit
  ``context.compacted``.
"""

from __future__ import annotations

from pathlib import Path
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


def _platform_yaml(root: Path) -> None:
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (root / sub).mkdir(exist_ok=True)
    (root / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
                "sandbox": {"variant": "native", "config": {}},
                "authorization": {
                    "variant": "rule_based",
                    "config": {"rules": [{"who": ".*", "what": ".*", "effect": "allow"}]},
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


async def _build(
    tmp_path: Path,
    *,
    compaction_provider: Any = None,
    llm_providers: dict[str, ProviderConfig] | None = None,
    llm: Any = None,
) -> tuple[AgentEngine, SessionStore]:
    _platform_yaml(tmp_path)
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
        llm=llm or default_llm,
        llm_providers_config=llm_providers or {},
        compaction_provider=compaction_provider,
    )
    return engine, store


def _config(*, agent_id: str = "ag_test") -> ResolvedAgentConfig:
    return ResolvedAgentConfig(
        schema_version="0.1",
        assembly_version="2",
        agent_id=AgentId(agent_id),
        name="test-agent",
        description="A test agent.",
        model=ModelSpec(provider="mock", model="mock-model"),
        prompt="You are a test agent.",
        io=IOSpec(mode="conversational"),
    )


async def _fresh_thread_ctx(store: SessionStore, agent_id: str = "ag_test") -> ExecutionContext:
    session = await store.create_session(agent_id, name="test-session")
    threads = await store.list_threads(session.session_id)
    return ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": "hello there"},
    )


@pytest.mark.asyncio
async def test_no_compaction_when_provider_is_none(tmp_path: Path) -> None:
    engine, store = await _build(tmp_path, compaction_provider=None)
    ctx = await _fresh_thread_ctx(store)
    await engine.execute(_config(), ctx)
    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    assert not any(e.type == EventType.CONTEXT_COMPACTED for e in events)


@pytest.mark.asyncio
async def test_no_compaction_when_context_length_is_none(tmp_path: Path) -> None:
    class _MockLLMHandle:
        async def __call__(self, request: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content=(
                    '{"schema_version": 1, "summary_text": "s", "refs": [], '
                    '"kept_files": [], "span": {}}'
                ),
                tool_calls=None,
                usage={},
                raw={},
            )

    async def factory(_spec: Any) -> Any:
        return _MockLLMHandle()

    provider = ReferenceSummaryCompactionProvider(
        llm_for_model=factory,
        config={},
    )
    engine, store = await _build(
        tmp_path,
        compaction_provider=provider,
        llm_providers={},  # no `mock` provider → context_length_for → None
    )
    ctx = await _fresh_thread_ctx(store)
    await engine.execute(_config(), ctx)
    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    assert not any(e.type == EventType.CONTEXT_COMPACTED for e in events)


@pytest.mark.asyncio
async def test_compaction_fires_when_estimate_exceeds_threshold(tmp_path: Path) -> None:
    class _MockLLMHandle:
        async def __call__(self, request: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content=(
                    '{"schema_version": 1, "summary_text": "prior turn was routine", '
                    '"refs": [], "kept_files": [], '
                    '"span": {"first_event_id": "ev_x", "last_event_id": "ev_y", '
                    '"event_count": 2}}'
                ),
                tool_calls=None,
                usage={},
                raw={},
            )

    async def factory(_spec: Any) -> Any:
        return _MockLLMHandle()

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
            # context_length tiny so ANY non-empty conversation blows past.
            models=[{"id": "mock-model", "context_length": 20}],
        ),
    }
    engine, store = await _build(
        tmp_path,
        compaction_provider=provider,
        llm_providers=llm_providers,
    )
    # First exchange: populate log.
    ctx1 = await _fresh_thread_ctx(store)
    await engine.execute(_config(), ctx1)
    # Second exchange on the same thread: trigger should fire.
    ctx2 = ExecutionContext(
        thread_id=ctx1.thread_id,
        trigger={
            "kind": "user_message",
            "content": "and another message adding pressure",
        },
    )
    await engine.execute(_config(), ctx2)
    events = [e async for e in store.read_path_to_leaf(ctx2.thread_id)]
    assert any(e.type == EventType.CONTEXT_COMPACTED for e in events), (
        [e.type.value for e in events]
    )
