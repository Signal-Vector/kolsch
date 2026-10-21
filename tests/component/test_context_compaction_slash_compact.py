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
from errors import ModelContextOverflow
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
        llm_providers_config=llm_providers
        or {
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
            content=self.reply,
            tool_calls=None,
            usage={},
            raw={},
        )


@pytest.mark.asyncio
async def test_manual_compact_forces_below_threshold(tmp_path: Path) -> None:
    """AC #9 — /compact fires even when auto trigger wouldn't."""
    recording = _RecordingLLM(
        '{"schema_version": 1, "summary_text": "s", "refs": [], '
        '"kept_files": [], "span": {"first_event_id": "e", '
        '"last_event_id": "f", "event_count": 1}}'
    )

    async def factory(_spec: Any, *, context: Any = None) -> Any:
        return recording

    provider = ReferenceSummaryCompactionProvider(
        llm_for_model=factory,
        config={},
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

    async def factory(_spec: Any, *, context: Any = None) -> Any:
        return recording

    provider = ReferenceSummaryCompactionProvider(
        llm_for_model=factory,
        config={},
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

    async def factory(_spec: Any, *, context: Any = None) -> Any:
        return recording

    provider = ReferenceSummaryCompactionProvider(
        llm_for_model=factory,
        config={},
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


# ---------------------------------------------------------------------------
# bug-113 — manual `/compact` overflow fallback ladder
# ---------------------------------------------------------------------------


class _OverflowSummariser:
    """Summariser stub that always raises ``ModelContextOverflow``.

    Models the real failure: the middle span is too large for the
    summariser's own context window. Counts calls so tests can assert
    the overflow short-circuit (bug-113) doesn't fan out into the full
    retry/keep-fraction ladder.
    """

    def __init__(self) -> None:
        self.calls: int = 0

    async def __call__(self, _request: LLMRequest) -> LLMResponse:
        self.calls += 1
        raise ModelContextOverflow(
            provider="mock",
            model="mock-summariser",
            requested_tokens=10_000,
        )


class _TransientRaisingSummariser:
    """Summariser stub that raises a NON-overflow error on every call."""

    def __init__(self) -> None:
        self.calls: int = 0

    async def __call__(self, _request: LLMRequest) -> LLMResponse:
        self.calls += 1
        msg = f"transient-{self.calls}"
        raise RuntimeError(msg)


async def _seed_turns(engine: AgentEngine, thread_id: Any, *, turns: int = 8) -> None:
    for turn in range(turns):
        ctx = ExecutionContext(
            thread_id=thread_id,
            trigger={"kind": "user_message", "content": f"turn {turn} " * 20},
        )
        await engine.execute(_config(), ctx)


@pytest.mark.asyncio
async def test_manual_compact_overflow_degrades_to_mechanical(tmp_path: Path) -> None:
    """bug-113 Test 1 — /compact on an overflowing middle emits a
    mechanical-truncation CONTEXT_COMPACTED (trigger=manual_truncation)
    instead of propagating ModelContextOverflow."""
    summariser = _OverflowSummariser()

    async def factory(_spec: Any, *, context: Any = None) -> Any:
        return summariser

    provider = ReferenceSummaryCompactionProvider(llm_for_model=factory, config={})
    engine, store = await _build(tmp_path, compaction_provider=provider)
    thread_id = await _fresh_thread(store)
    await _seed_turns(engine, thread_id)

    emitted = await engine.manual_compact(config=_config(), thread_id=thread_id)

    assert emitted is not None
    assert emitted.type == EventType.CONTEXT_COMPACTED
    # Top-level AND nested trigger both mark a MANUAL mechanical fallback.
    assert emitted.data.get("trigger") == "manual_truncation"
    assert (emitted.data.get("summary") or {}).get("trigger") == "manual_truncation"
    # Provenance marker so /why-compact can tell truncation from summary.
    assert emitted.data.get("pre_compaction", {}).get("fallback") == (
        "mechanical_truncation"
    )
    # The dropped span is covered so context.reference_lookup still works.
    assert emitted.data.get("compacted_event_ids")


@pytest.mark.asyncio
async def test_manual_compact_overflow_short_circuits_summariser(
    tmp_path: Path,
) -> None:
    """bug-113 Test 2 — a ModelContextOverflow short-circuits straight to
    tier-2 mechanical truncation with exactly one summariser call (no
    retry, no walking the remaining keep-fractions)."""
    overflow = _OverflowSummariser()

    async def overflow_factory(_spec: Any, *, context: Any = None) -> Any:
        return overflow

    provider = ReferenceSummaryCompactionProvider(
        llm_for_model=overflow_factory, config={}
    )
    engine, store = await _build(tmp_path, compaction_provider=provider)
    thread_id = await _fresh_thread(store)
    await _seed_turns(engine, thread_id)

    # Ignore summariser calls the per-exchange auto-check made while
    # seeding — measure only the manual /compact invocation.
    overflow.calls = 0
    emitted = await engine.manual_compact(config=_config(), thread_id=thread_id)
    assert emitted is not None
    assert overflow.calls == 1


@pytest.mark.asyncio
async def test_manual_compact_transient_error_retries_then_truncates(
    tmp_path: Path,
) -> None:
    """bug-113 Test 2 (contrast) — a NON-overflow (transient) error keeps
    the existing retry behaviour: the first keep-fraction is attempted
    twice before the ladder falls through to mechanical truncation."""
    transient = _TransientRaisingSummariser()

    async def transient_factory(_spec: Any, *, context: Any = None) -> Any:
        return transient

    provider = ReferenceSummaryCompactionProvider(
        llm_for_model=transient_factory, config={}
    )
    engine, store = await _build(tmp_path, compaction_provider=provider)
    thread_id = await _fresh_thread(store)
    await _seed_turns(engine, thread_id)

    transient.calls = 0
    emitted = await engine.manual_compact(config=_config(), thread_id=thread_id)
    assert emitted is not None
    # Mechanical fallback still lands (non-LLM), so /compact succeeds.
    assert emitted.data.get("trigger") == "manual_truncation"
    # Two attempts within the first keep-fraction before the fall-through.
    assert transient.calls == 2


@pytest.mark.asyncio
async def test_manual_compact_no_net_savings_keeps_summary(tmp_path: Path) -> None:
    """bug-113 Test 3 — a fittable tier-1 summary with no net savings is
    emitted as-is (trigger=manual), NOT downgraded to mechanical
    truncation and NOT swallowed by an auto-cadence skip event."""
    # A huge summary_text projects tokens_after > 0.90 * tokens_before,
    # tripping the no-net-savings guard.
    big_summary = "word " * 4000
    recording = _RecordingLLM(
        '{"schema_version": 1, "summary_text": "'
        + big_summary
        + '", "refs": [], "kept_files": [], "span": {"first_event_id": "e", '
        '"last_event_id": "f", "event_count": 1}}'
    )

    async def factory(_spec: Any, *, context: Any = None) -> Any:
        return recording

    provider = ReferenceSummaryCompactionProvider(llm_for_model=factory, config={})
    engine, store = await _build(tmp_path, compaction_provider=provider)
    thread_id = await _fresh_thread(store)
    await _seed_turns(engine, thread_id)

    # Baseline any skip events the per-exchange auto-check emitted while
    # seeding so we measure only what the manual /compact adds.
    skipped_before = len(
        [
            e
            async for e in store.read_path_to_leaf(thread_id)
            if e.type == EventType.CONTEXT_COMPACTION_SKIPPED
        ]
    )
    emitted = await engine.manual_compact(config=_config(), thread_id=thread_id)

    assert emitted is not None
    # The real (LLM) summary is emitted, NOT a mechanical truncation.
    assert emitted.data.get("trigger") == "manual"
    assert "fallback" not in (emitted.data.get("pre_compaction") or {})
    # The manual path emitted no auto-flavoured skip event of its own.
    skipped_after = len(
        [
            e
            async for e in store.read_path_to_leaf(thread_id)
            if e.type == EventType.CONTEXT_COMPACTION_SKIPPED
        ]
    )
    assert skipped_after == skipped_before


@pytest.mark.asyncio
async def test_manual_compact_preview_overflow_never_mutates_context(
    tmp_path: Path,
) -> None:
    """bug-113 Test 4 — /compact --preview that overflows surfaces the
    failure (re-raise) and NEVER emits a live (preview=False) mechanical
    compaction."""
    summariser = _OverflowSummariser()

    async def factory(_spec: Any, *, context: Any = None) -> Any:
        return summariser

    provider = ReferenceSummaryCompactionProvider(llm_for_model=factory, config={})
    engine, store = await _build(tmp_path, compaction_provider=provider)
    thread_id = await _fresh_thread(store)
    await _seed_turns(engine, thread_id)

    async def _count_compacted() -> int:
        return len(
            [
                e
                async for e in store.read_path_to_leaf(thread_id)
                if e.type == EventType.CONTEXT_COMPACTED
            ]
        )

    compacted_before = await _count_compacted()
    with pytest.raises(ModelContextOverflow):
        await engine.manual_compact(
            config=_config(),
            thread_id=thread_id,
            preview=True,
        )

    # Preview stayed strictly read-only — the overflow surfaced as a
    # raise and NO new CONTEXT_COMPACTED landed (tier-2 mechanical
    # truncation, which forces preview=False, was never invoked).
    assert await _count_compacted() == compacted_before
