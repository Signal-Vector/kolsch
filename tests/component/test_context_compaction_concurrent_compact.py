"""feat-054 Phase 4 — thread-safety regression on ``manual_compact``.

Phase 3 reviewer finding #1: ``AgentEngine.manual_compact`` and
``manual_uncompact`` need to serialise against in-flight exchanges on
the same thread, otherwise a compaction's ``compacted_event_ids`` can
capture a stale leaf (an exchange appending events between our
``read_path_to_leaf`` and ``append_event`` produces a compaction whose
span excludes fresh events, leaving a jagged "past-me, not-around-me"
state on the log).

The regression covers the concurrent-invocation case: fire
``manual_compact`` and ``engine.execute`` on the same thread from two
coroutines that race, then assert every compaction event on the
thread lists ONLY event_ids that precede its own append point on the
log (i.e., the compaction's span is a prefix, not a snapshot that
grows around it).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from pathlib import Path


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
                    "config": {
                        "rules": [{"who": ".*", "what": ".*", "effect": "allow"}]
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


@pytest.mark.asyncio
async def test_manual_compact_serialises_with_concurrent_exchange(
    tmp_path: Path,
) -> None:
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

    async def default_llm(_request: LLMRequest) -> LLMResponse:
        return LLMResponse(content="ok", tool_calls=None, usage={}, raw={})

    class _Summariser:
        async def __call__(self, _request: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content=(
                    '{"schema_version": 1, "summary_text": "s", '
                    '"refs": [], "kept_files": [], '
                    '"span": {"first_event_id": "ev_x", '
                    '"last_event_id": "ev_y", "event_count": 2}}'
                ),
                tool_calls=None,
                usage={},
                raw={},
            )

    async def factory(_spec: Any, *, context: Any = None) -> Any:
        return _Summariser()

    provider = ReferenceSummaryCompactionProvider(llm_for_model=factory, config={})
    llm_providers = {
        "mock": ProviderConfig(
            name="mock",
            type="mock",
            api_key_secret=None,
            base_url=None,
            timeout_seconds=None,
            extras={},
            # Tiny context so `keep_tail_tokens` is minimal — the
            # provider's tail-boundary walk consumes only a handful
            # of events, leaving a non-empty middle span for
            # manual_compact to compact against.
            models=[{"id": "mock-model", "context_length": 20}],
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
    config = ResolvedAgentConfig(
        schema_version="0.1",
        assembly_version="2",
        agent_id=AgentId("ag_test"),
        name="test-agent",
        description="A test agent.",
        model=ModelSpec(provider="mock", model="mock-model"),
        prompt="You are a test agent.",
        io=IOSpec(mode="conversational"),
    )

    session = await store.create_session("ag_test", name="test-session")
    thread = (await store.list_threads(session.session_id))[0]
    # Seed the thread with enough exchanges that manual_compact has a
    # non-empty middle span. Auto-compaction may or may not fire in
    # these; the race we're testing is between manual_compact (the
    # explicit call below) and a concurrent engine.execute.
    for i in range(5):
        await engine.execute(
            config,
            ExecutionContext(
                thread_id=thread.thread_id,
                trigger={
                    "kind": "user_message",
                    "content": f"seed turn {i} " * 20,
                },
            ),
        )

    # Race: fire manual_compact + a fresh execute on the same thread
    # concurrently. The per-thread lock the engine holds in
    # `_lock_for_thread` MUST serialise the two so the compaction
    # emitted by manual_compact captures a coherent prefix, and the
    # execute's own emits don't slip inside the compaction's window.
    async def run_compact() -> Any:
        return await engine.manual_compact(config=config, thread_id=thread.thread_id)

    async def run_execute() -> Any:
        return await engine.execute(
            config,
            ExecutionContext(
                thread_id=thread.thread_id,
                trigger={
                    "kind": "user_message",
                    "content": "concurrent user turn",
                },
            ),
        )

    await asyncio.gather(run_compact(), run_execute())

    events = [e async for e in store.read_path_to_leaf(thread.thread_id)]
    # Build the (event_id → chronological index) map from the append
    # order the store returns.
    index_by_id = {str(e.event_id): i for i, e in enumerate(events)}
    compactions = [e for e in events if e.type == EventType.CONTEXT_COMPACTED]
    assert compactions, (
        "manual_compact should have emitted at least one CONTEXT_COMPACTED "
        "for the seeded thread"
    )
    # Every event_id inside a compaction's span MUST have been on the
    # log BEFORE the compaction event itself. Without the lock, a
    # concurrent execute would slip a fresh user.message/model.request
    # into the store between manual_compact's read and its emit,
    # producing a compaction whose span excludes those interleaved
    # events — the position of the compaction event on the path would
    # NOT be strictly after every id in `compacted_event_ids`. This
    # regression is the finding Phase 3 reviewer flagged.
    for c in compactions:
        c_pos = index_by_id[str(c.event_id)]
        span = c.data.get("compacted_event_ids") or []
        for eid in span:
            eid_pos = index_by_id.get(str(eid))
            assert eid_pos is not None, (
                f"compaction referenced event_id {eid!r} that is not on "
                f"the thread — the compaction captured a stale snapshot"
            )
            assert eid_pos < c_pos, (
                f"compaction {str(c.event_id)!r} references event_id "
                f"{eid!r} at position {eid_pos} AFTER the compaction's "
                f"own position {c_pos} — the per-thread lock did not "
                f"serialise concurrent emits"
            )
