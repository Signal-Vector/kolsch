"""feat-054 Phase 3 — ``/compact`` slash-command handler tests.

Covers three behaviours of :meth:`AgentEngine.manual_compact` — the
seam the ``/compact`` handler invokes — end-to-end against a real
:class:`AgentEngine` + :class:`SessionStore`:

- Bare ``/compact`` forces compaction regardless of threshold and
  emits a ``CONTEXT_COMPACTED`` event with ``trigger="manual"``.
- ``/compact --preview`` emits ``trigger="manual_preview"`` and sets
  ``preview=True`` on the payload.
- ``/compact --instructions "..."`` threads the extra guidance into
  the summariser LLM prompt (verified by capturing the prompt).

Uses the same synthesizer harness as
:mod:`tests.component.test_context_compaction_engine_trigger`.
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


async def _build(
    tmp_path: Path,
    *,
    compaction_provider: Any = None,
    llm_providers: dict[str, ProviderConfig] | None = None,
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
        llm=default_llm,
        llm_providers_config=llm_providers or {
            # Tiny context_length so keep_tail (budget * 0.20) is a
            # handful of tokens — anything beyond a single event
            # spills into the middle span.
            "mock": ProviderConfig(
                name="mock",
                type="mock",
                api_key_secret=None,
                base_url=None,
                timeout_seconds=None,
                extras={},
                models=[{"id": "mock-model", "context_length": 200}],
            ),
        },
        compaction_provider=compaction_provider,
    )
    return engine, store


async def _fresh_thread(store: SessionStore) -> Any:
    session = await store.create_session("ag_test", name="test-session")
    threads = await store.list_threads(session.session_id)
    return threads[0].thread_id


class _RecordingLLM:
    """Captures the last summariser request for prompt assertions."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[LLMRequest] = []

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        return LLMResponse(
            content=self.reply, tool_calls=None, usage={}, raw={},
        )


@pytest.mark.asyncio
async def test_manual_compact_forces_below_threshold(tmp_path: Path) -> None:
    """AC #9 — /compact fires even when auto trigger wouldn't."""
    recording = _RecordingLLM(
        '{"schema_version": 1, "summary_text": "s", "refs": [], '
        '"kept_files": [], "span": {"first_event_id": "e", '
        '"last_event_id": "f", "event_count": 1}}'
    )

    async def factory(_spec: Any) -> Any:
        return recording

    provider = ReferenceSummaryCompactionProvider(
        llm_for_model=factory, config={},
    )
    engine, store = await _build(tmp_path, compaction_provider=provider)
    thread_id = await _fresh_thread(store)

    # Populate several exchanges so the middle span is non-empty
    # (the tail keep-fraction covers ~one user/assistant round-trip
    # given the tiny context_length we've wired for these tests).
    for turn in range(5):
        ctx = ExecutionContext(
            thread_id=thread_id,
            trigger={
                "kind": "user_message",
                "content": f"turn {turn} " * 20,
            },
        )
        await engine.execute(_config(), ctx)

    # Force a compaction — the trigger marker on the emitted event
    # tells us the manual path fired (auto-fired compactions would
    # carry trigger="auto").
    emitted = await engine.manual_compact(
        config=_config(),
        thread_id=thread_id,
    )
    assert emitted is not None
    assert emitted.type == EventType.CONTEXT_COMPACTED
    assert emitted.data.get("trigger") == "manual"
    assert emitted.data.get("preview") is False


@pytest.mark.asyncio
async def test_manual_compact_preview_sets_flag(tmp_path: Path) -> None:
    """AC #9 — /compact --preview emits preview=True + manual_preview trigger."""
    recording = _RecordingLLM(
        '{"schema_version": 1, "summary_text": "s", "refs": [], '
        '"kept_files": [], "span": {"first_event_id": "e", '
        '"last_event_id": "f", "event_count": 1}}'
    )

    async def factory(_spec: Any) -> Any:
        return recording

    provider = ReferenceSummaryCompactionProvider(
        llm_for_model=factory, config={},
    )
    engine, store = await _build(tmp_path, compaction_provider=provider)
    thread_id = await _fresh_thread(store)
    for turn in range(5):
        ctx = ExecutionContext(
            thread_id=thread_id,
            trigger={
                "kind": "user_message",
                "content": f"turn {turn} " * 20,
            },
        )
        await engine.execute(_config(), ctx)

    emitted = await engine.manual_compact(
        config=_config(),
        thread_id=thread_id,
        preview=True,
    )
    assert emitted is not None
    assert emitted.data.get("trigger") == "manual_preview"
    assert emitted.data.get("preview") is True


@pytest.mark.asyncio
async def test_manual_compact_threads_extra_instructions(tmp_path: Path) -> None:
    """AC #9 — --instructions flows into the summariser prompt."""
    recording = _RecordingLLM(
        '{"schema_version": 1, "summary_text": "s", "refs": [], '
        '"kept_files": [], "span": {"first_event_id": "e", '
        '"last_event_id": "f", "event_count": 1}}'
    )

    async def factory(_spec: Any) -> Any:
        return recording

    provider = ReferenceSummaryCompactionProvider(
        llm_for_model=factory, config={},
    )
    engine, store = await _build(tmp_path, compaction_provider=provider)
    thread_id = await _fresh_thread(store)
    for turn in range(5):
        ctx = ExecutionContext(
            thread_id=thread_id,
            trigger={
                "kind": "user_message",
                "content": f"turn {turn} " * 20,
            },
        )
        await engine.execute(_config(), ctx)

    guidance = "focus on file edits over prose"
    emitted = await engine.manual_compact(
        config=_config(),
        thread_id=thread_id,
        extra_instructions=guidance,
    )
    assert emitted is not None
    # Recorded summariser prompt should carry the guidance verbatim.
    assert recording.calls, "summariser was not invoked"
    joined = "\n".join(m.content for m in recording.calls[-1].messages)
    assert guidance in joined


@pytest.mark.asyncio
async def test_manual_compact_returns_none_when_provider_disabled(
    tmp_path: Path,
) -> None:
    """AC #9 — /compact silently skips when compaction is disabled."""
    engine, store = await _build(tmp_path, compaction_provider=None)
    thread_id = await _fresh_thread(store)
    result = await engine.manual_compact(
        config=_config(),
        thread_id=thread_id,
    )
    assert result is None
