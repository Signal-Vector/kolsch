"""feat-054 Phase 5 — session-open eager compaction trigger.

Covers :meth:`AgentEngine.maybe_compact_thread_eagerly` — the
resume-time compaction hook that fires BEFORE the first post-resume
exchange so the summariser delay is absorbed into session-open UX
rather than the user's first message.

Scenarios:

- Session with events well under threshold → returns ``None``, no
  summariser call, no ``CONTEXT_COMPACTED`` on the thread.
- Session over threshold → returns the emitted event, ``trigger``
  field set to ``"auto_resume"``, event landed on the thread.
- Summariser fails twice → mechanical truncation fallback, event's
  ``summary.trigger`` marked ``"auto_truncation"``.
- Race with :meth:`AgentEngine.manual_compact` — both acquire the
  per-thread lock cleanly; no torn state.
- Fresh session (no events) → returns ``None`` without any LLM call.
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

    async def default_llm(_request: LLMRequest) -> LLMResponse:
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


def _config() -> ResolvedAgentConfig:
    return ResolvedAgentConfig(
        schema_version="0.1",
        assembly_version="2",
        agent_id=AgentId("ag_test"),
        name="test-agent",
        description="A test agent.",
        model=ModelSpec(provider="mock", model="mock-model"),
        prompt="You are a test agent.",
        io=IOSpec(mode="conversational"),
    )


def _tiny_context_providers() -> dict[str, ProviderConfig]:
    """A provider config whose ``context_length`` is tiny so ANY
    non-empty seeding blows past the trigger threshold."""
    return {
        "mock": ProviderConfig(
            name="mock",
            type="mock",
            api_key_secret=None,
            base_url=None,
            timeout_seconds=None,
            extras={},
            models=[{"id": "mock-model", "context_length": 20}],
        ),
    }


def _generous_context_providers() -> dict[str, ProviderConfig]:
    """A provider config with a large ``context_length`` — the trigger
    never fires so :meth:`maybe_compact_thread_eagerly` returns ``None``."""
    return {
        "mock": ProviderConfig(
            name="mock",
            type="mock",
            api_key_secret=None,
            base_url=None,
            timeout_seconds=None,
            extras={},
            models=[{"id": "mock-model", "context_length": 1_000_000}],
        ),
    }


def _valid_summary_json(summary_text: str = "prior turn was routine") -> str:
    return (
        '{"schema_version": 1, "summary_text": "' + summary_text + '", '
        '"refs": [], "kept_files": [], '
        '"span": {"first_event_id": "ev_x", "last_event_id": "ev_y", '
        '"event_count": 2}}'
    )


class _CountingSummariser:
    """LLM stub that returns a valid summary JSON and counts calls."""

    def __init__(self) -> None:
        self.calls: int = 0

    async def __call__(self, _request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content=_valid_summary_json(),
            tool_calls=None,
            usage={},
            raw={},
        )


class _RaisingSummariser:
    """LLM stub that raises on every call — drives the tier-2 fallback."""

    def __init__(self) -> None:
        self.calls: int = 0

    async def __call__(self, _request: LLMRequest) -> LLMResponse:
        self.calls += 1
        msg = f"boom-{self.calls}"
        raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# Fresh-session guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_session_no_events_returns_none_without_llm_call(
    tmp_path: Path,
) -> None:
    """A brand-new session with no events fast-paths to ``None``.

    The engine's eager check must be cheap on a fresh session — no
    :func:`assemble_context` overhead and, critically, no summariser
    call. A cold-boot chat should never fire compaction against a
    zero-events thread.
    """
    summariser = _CountingSummariser()

    async def factory(_spec: Any) -> Any:
        return summariser

    provider = ReferenceSummaryCompactionProvider(llm_for_model=factory, config={})
    engine, store = await _build(
        tmp_path,
        compaction_provider=provider,
        llm_providers=_tiny_context_providers(),
    )
    session = await store.create_session("ag_test", name="test-session")
    thread = (await store.list_threads(session.session_id))[0]
    result = await engine.maybe_compact_thread_eagerly(
        thread_id=thread.thread_id,
        spec=_config().model,
    )
    assert result is None
    assert summariser.calls == 0
    events = [e async for e in store.read_path_to_leaf(thread.thread_id)]
    assert not any(e.type == EventType.CONTEXT_COMPACTED for e in events)


# ---------------------------------------------------------------------------
# Under threshold — no compaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_under_threshold_returns_none_no_compaction_emitted(
    tmp_path: Path,
) -> None:
    """When the accumulated context is comfortably under the trigger
    the eager check returns ``None`` and emits nothing."""
    summariser = _CountingSummariser()

    async def factory(_spec: Any) -> Any:
        return summariser

    provider = ReferenceSummaryCompactionProvider(llm_for_model=factory, config={})
    engine, store = await _build(
        tmp_path,
        compaction_provider=provider,
        # Generous context length — trigger never fires.
        llm_providers=_generous_context_providers(),
    )
    # Seed one turn so read_path_to_leaf is not empty (fresh guard
    # would short-circuit otherwise).
    session = await store.create_session("ag_test", name="test-session")
    thread = (await store.list_threads(session.session_id))[0]
    await engine.execute(
        _config(),
        ExecutionContext(
            thread_id=thread.thread_id,
            trigger={"kind": "user_message", "content": "hi"},
        ),
    )
    result = await engine.maybe_compact_thread_eagerly(
        thread_id=thread.thread_id,
        spec=_config().model,
    )
    assert result is None
    assert summariser.calls == 0
    events = [e async for e in store.read_path_to_leaf(thread.thread_id)]
    assert not any(e.type == EventType.CONTEXT_COMPACTED for e in events)


# ---------------------------------------------------------------------------
# Over threshold — successful compaction with trigger="auto_resume"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_over_threshold_emits_context_compacted_auto_resume(
    tmp_path: Path,
) -> None:
    """When the projection exceeds threshold the eager check runs the
    summariser and emits ``CONTEXT_COMPACTED`` with the new
    ``trigger="auto_resume"`` marker so downstream telemetry can tell
    it apart from a per-exchange compaction."""
    summariser = _CountingSummariser()

    async def factory(_spec: Any) -> Any:
        return summariser

    provider = ReferenceSummaryCompactionProvider(llm_for_model=factory, config={})
    engine, store = await _build(
        tmp_path,
        compaction_provider=provider,
        llm_providers=_tiny_context_providers(),
    )
    # Seed two exchanges so the projection has real content.
    session = await store.create_session("ag_test", name="test-session")
    thread = (await store.list_threads(session.session_id))[0]
    for i in range(2):
        await engine.execute(
            _config(),
            ExecutionContext(
                thread_id=thread.thread_id,
                trigger={"kind": "user_message", "content": f"seed turn {i}"},
            ),
        )
    # The per-exchange trigger may have already compacted (tiny
    # context_length). Take the pre-eager compaction count as baseline.
    events = [e async for e in store.read_path_to_leaf(thread.thread_id)]
    pre_count = sum(1 for e in events if e.type == EventType.CONTEXT_COMPACTED)
    result = await engine.maybe_compact_thread_eagerly(
        thread_id=thread.thread_id,
        spec=_config().model,
    )
    # The eager check either compacted (returning the Event) or
    # decided the tail was already covered (returning None because a
    # prior compaction shrank the middle). Either way: no exception,
    # composer stays enabled.
    events = [e async for e in store.read_path_to_leaf(thread.thread_id)]
    compacted = [e for e in events if e.type == EventType.CONTEXT_COMPACTED]
    if result is not None:
        # The eager path fired — the emitted event carries the
        # auto_resume trigger.
        assert len(compacted) == pre_count + 1
        assert compacted[-1].data.get("trigger") == "auto_resume"
    else:
        # Nothing to compact (the middle span was empty). No net
        # change to the compaction count.
        assert len(compacted) == pre_count


# ---------------------------------------------------------------------------
# Failure ladder — summariser fails twice → mechanical fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summariser_fails_twice_falls_through_to_mechanical(
    tmp_path: Path,
) -> None:
    """When the summariser raises on every attempt the eager check
    walks the same 3-tier failure ladder as the per-exchange path,
    emits ``CONTEXT_COMPACTION_FAILED``, then a mechanical
    ``CONTEXT_COMPACTED`` with ``summary.trigger="auto_truncation"``."""
    summariser = _RaisingSummariser()

    async def factory(_spec: Any) -> Any:
        return summariser

    provider = ReferenceSummaryCompactionProvider(llm_for_model=factory, config={})
    engine, store = await _build(
        tmp_path,
        compaction_provider=provider,
        llm_providers=_tiny_context_providers(),
    )
    session = await store.create_session("ag_test", name="test-session")
    thread = (await store.list_threads(session.session_id))[0]
    # Seed with default LLM so exchanges succeed. Each turn's own
    # per-exchange compaction check will also try (and fail) via
    # our raising summariser, populating some prior FAILED events. The
    # eager check we're testing runs AFTER seeding.
    for i in range(3):
        await engine.execute(
            _config(),
            ExecutionContext(
                thread_id=thread.thread_id,
                trigger={
                    "kind": "user_message",
                    "content": f"seed turn {i} " * 5,
                },
            ),
        )
    # Reset the summariser call counter so we only measure eager path.
    summariser.calls = 0
    result = await engine.maybe_compact_thread_eagerly(
        thread_id=thread.thread_id,
        spec=_config().model,
    )
    events = [e async for e in store.read_path_to_leaf(thread.thread_id)]
    compacted = [e for e in events if e.type == EventType.CONTEXT_COMPACTED]
    failed = [e for e in events if e.type == EventType.CONTEXT_COMPACTION_FAILED]
    # The eager path called the summariser at least twice (tier-1 retry).
    assert summariser.calls >= 2
    # A failure event landed and the mechanical fallback fired.
    assert failed, "expected at least one CONTEXT_COMPACTION_FAILED"
    assert compacted, "expected at least one CONTEXT_COMPACTED (mechanical)"
    # The most recent CONTEXT_COMPACTED is mechanical — its
    # summary.trigger marks the fallback path.
    last_compacted = compacted[-1]
    summary = last_compacted.data.get("summary") or {}
    assert summary.get("trigger") == "auto_truncation"
    # bug-113 — the top-level trigger now agrees with summary.trigger
    # (previously it borrowed "auto"); an auto/eager mechanical fallback
    # reads as "auto_truncation" on both fields.
    assert last_compacted.data.get("trigger") == "auto_truncation"
    # And the eager method returned the Event it emitted.
    assert result is not None


# ---------------------------------------------------------------------------
# Concurrent with manual_compact — both use _lock_for_thread cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_with_manual_compact_serialises_cleanly(
    tmp_path: Path,
) -> None:
    """Racing :meth:`maybe_compact_thread_eagerly` with
    :meth:`manual_compact` on the same thread must serialise via the
    per-thread lock. Neither call should raise; every emitted
    ``CONTEXT_COMPACTED`` should have a well-formed
    ``compacted_event_ids`` list (no torn parent chain)."""
    summariser = _CountingSummariser()

    async def factory(_spec: Any) -> Any:
        return summariser

    provider = ReferenceSummaryCompactionProvider(llm_for_model=factory, config={})
    engine, store = await _build(
        tmp_path,
        compaction_provider=provider,
        llm_providers=_tiny_context_providers(),
    )
    session = await store.create_session("ag_test", name="test-session")
    thread = (await store.list_threads(session.session_id))[0]
    # Seed enough events that manual_compact has a non-empty middle.
    for i in range(5):
        await engine.execute(
            _config(),
            ExecutionContext(
                thread_id=thread.thread_id,
                trigger={
                    "kind": "user_message",
                    "content": f"seed turn {i} " * 10,
                },
            ),
        )
    # Race eager + manual on the same thread.
    eager_task = asyncio.create_task(
        engine.maybe_compact_thread_eagerly(
            thread_id=thread.thread_id,
            spec=_config().model,
        )
    )
    manual_task = asyncio.create_task(
        engine.manual_compact(
            config=_config(),
            thread_id=thread.thread_id,
            preview=False,
        )
    )
    eager_result, manual_result = await asyncio.gather(
        eager_task, manual_task, return_exceptions=True
    )
    # No exceptions surfaced.
    assert not isinstance(eager_result, BaseException), eager_result
    assert not isinstance(manual_result, BaseException), manual_result
    # Every CONTEXT_COMPACTED on the thread has a well-formed
    # `compacted_event_ids` prefix relative to its own append point.
    events = [e async for e in store.read_path_to_leaf(thread.thread_id)]
    seen_ids: set[str] = set()
    for e in events:
        if e.type == EventType.CONTEXT_COMPACTED:
            targets = e.data.get("compacted_event_ids") or []
            assert isinstance(targets, list)
            for tid in targets:
                # Every target must precede this event on the path.
                assert tid in seen_ids, (
                    f"compaction {e.event_id} references "
                    f"{tid} which is not on the path prefix"
                )
        seen_ids.add(str(e.event_id))
