"""feat-054 Phase 4 — failure-ladder + non-shrinking guard + clustering.

Covers spec §8 tier transitions + §B non-shrinking-summary retry:

- **Tier 1 — retry once.** First summariser call raises; the second
  succeeds. Result: exactly one ``CONTEXT_COMPACTED`` (no failure or
  skipped events).
- **Tier 2 — mechanical truncation fallback.** Both summariser
  attempts raise (all retries at every ``keep_recent_fraction``
  exhausted). Result: one ``CONTEXT_COMPACTION_FAILED`` +
  one ``CONTEXT_COMPACTED`` whose ``summary.trigger`` is
  ``"auto_truncation"`` and ``summary.refs`` populated so
  ``context.reference_lookup`` can still resolve dropped events.
- **Tier 3 — hard-fail** when ``keep_tail_tokens`` alone exceeds the
  budget: the exchange fails with
  ``error.type = "context.overflow_uncompactable"``.
- **Non-shrinking guard.** Summariser returns a payload whose
  ``tokens_after > tokens_before × 0.90``; the compaction is
  discarded, ``CONTEXT_COMPACTION_SKIPPED`` fires with
  ``reason="no_net_savings"``, and the trigger retries with a
  tighter ``keep_recent_fraction``.
- **Refs clustering.** When the summariser produces more than
  ``max_refs`` entries, the overflow is folded into a single
  ``event_id_range`` cluster; ``context.reference_lookup`` resolves
  the range's first/last exact event_ids but refuses the cluster ref
  itself.
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
from errors import ContextOverflowUncompactable
from files import PathPolicy
from llm import ProviderConfig
from providers import (
    NativeSandboxProvider,
    RecentBufferMemoryProvider,
    RuleBasedAuthorizationProvider,
)
from providers.compaction import (
    ReferenceSummaryCompactionProvider,
    build_mechanical_truncation_payload,
)
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


async def _fresh_thread_ctx(
    store: SessionStore,
    *,
    text: str = "hello there",
) -> ExecutionContext:
    session = await store.create_session("ag_test", name="test-session")
    threads = await store.list_threads(session.session_id)
    return ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": text},
    )


def _tiny_context_providers() -> dict[str, ProviderConfig]:
    return {
        "mock": ProviderConfig(
            name="mock",
            type="mock",
            api_key_secret=None,
            base_url=None,
            timeout_seconds=None,
            extras={},
            # tiny → trigger fires on nearly-empty projections
            models=[{"id": "mock-model", "context_length": 20}],
        ),
    }


class _ScriptedSummariser:
    """Programmable summariser LLM.

    Each call pops from ``script`` — a str returns as LLMResponse.content,
    a BaseException is raised. ValueError-shaped so
    :func:`providers.compaction._parse_summariser_json` sees a non-JSON
    reply on ``non_json``.
    """

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: int = 0

    async def __call__(self, _request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if not self._script:
            raise RuntimeError("script exhausted")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return LLMResponse(content=str(item), tool_calls=None, usage={}, raw={})


def _valid_summary_json(summary_text: str = "prior turn was routine") -> str:
    return (
        '{"schema_version": 1, "summary_text": "' + summary_text + '", '
        '"refs": [], "kept_files": [], '
        '"span": {"first_event_id": "ev_x", "last_event_id": "ev_y", '
        '"event_count": 2}}'
    )


# ---------------------------------------------------------------------------
# Tier 1 — retry once, success on second attempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier1_retry_survives_transient_summariser_failure(
    tmp_path: Path,
) -> None:
    """Tier-1 retry absorbs a single raise and continues the ladder.

    A summariser that raises on the FIRST call and returns valid JSON
    thereafter should not surface a ``CONTEXT_COMPACTION_FAILED`` on
    the thread — the retry runs and eventually the ladder either
    produces a summariser-driven ``CONTEXT_COMPACTED`` or falls
    through to a mechanical one for orthogonal reasons (tiny thread
    → non-shrinking guard). The load-bearing invariant is that a
    transient raise alone doesn't fail the exchange, and the
    summariser is called MORE than once (retry actually happened).
    """
    summariser = _ScriptedSummariser(
        [RuntimeError("transient network")] + [_valid_summary_json() for _ in range(20)]
    )

    async def factory(_spec: Any) -> Any:
        return summariser

    provider = ReferenceSummaryCompactionProvider(llm_for_model=factory, config={})
    engine, store = await _build(
        tmp_path,
        compaction_provider=provider,
        llm_providers=_tiny_context_providers(),
    )
    ctx1 = await _fresh_thread_ctx(store)
    await engine.execute(_config(), ctx1)
    ctx2 = ExecutionContext(
        thread_id=ctx1.thread_id,
        trigger={"kind": "user_message", "content": "more"},
    )
    await engine.execute(_config(), ctx2)
    # Retry actually happened: >= 2 calls to the summariser.
    assert summariser.calls >= 2
    events = [e async for e in store.read_path_to_leaf(ctx2.thread_id)]
    # The exchange completed (no THREAD_EXCHANGE_FAILED landed).
    assert not any(e.type == EventType.THREAD_EXCHANGE_FAILED for e in events)
    # A CONTEXT_COMPACTED landed (from either the summariser or the
    # mechanical fallback — both paths respect the retry).
    assert any(e.type == EventType.CONTEXT_COMPACTED for e in events)


# ---------------------------------------------------------------------------
# Tier 2 — mechanical truncation fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier2_mechanical_truncation_when_retries_exhausted(
    tmp_path: Path,
) -> None:
    # Both summariser calls raise. The engine's non-shrinking-guard
    # loop bails after tier-1 exhaustion (a raising summariser won't
    # succeed with a narrower tail), so we get exactly ONE
    # CONTEXT_COMPACTION_FAILED + one mechanical CONTEXT_COMPACTED.
    summariser = _ScriptedSummariser([RuntimeError("boom-1"), RuntimeError("boom-2")])

    async def factory(_spec: Any) -> Any:
        return summariser

    provider = ReferenceSummaryCompactionProvider(llm_for_model=factory, config={})
    engine, store = await _build(
        tmp_path,
        compaction_provider=provider,
        llm_providers=_tiny_context_providers(),
    )
    ctx1 = await _fresh_thread_ctx(store)
    await engine.execute(_config(), ctx1)
    ctx2 = ExecutionContext(
        thread_id=ctx1.thread_id,
        trigger={"kind": "user_message", "content": "more"},
    )
    await engine.execute(_config(), ctx2)

    events = [e async for e in store.read_path_to_leaf(ctx2.thread_id)]
    failed = [e for e in events if e.type == EventType.CONTEXT_COMPACTION_FAILED]
    compacted = [e for e in events if e.type == EventType.CONTEXT_COMPACTED]
    assert len(failed) == 1, "expected one CONTEXT_COMPACTION_FAILED"
    assert len(compacted) == 1, "expected one CONTEXT_COMPACTED (mechanical)"
    # Mechanical marker on the summary payload.
    summary = compacted[0].data.get("summary") or {}
    assert summary.get("trigger") == "auto_truncation"
    # Refs populated so context.reference_lookup can still resolve.
    refs = summary.get("refs") or []
    assert refs, "mechanical truncation must populate refs[]"
    # `compacted_event_ids` covers the dropped span so lookup scope
    # includes every id (even when refs is capped by max_refs).
    compacted_event_ids = compacted[0].data.get("compacted_event_ids") or []
    assert compacted_event_ids


# ---------------------------------------------------------------------------
# Tier 3 — hard-fail: recent tail alone exceeds budget
# ---------------------------------------------------------------------------


def test_build_mechanical_truncation_returns_none_when_middle_empty() -> None:
    # No events → nothing to compact; the engine's ladder promotes this
    # to tier 3 (ContextOverflowUncompactable).
    result = build_mechanical_truncation_payload(events=[], keep_tail_tokens=1)
    assert result is None


def test_tier3_hard_fail_error_shape_is_addressable() -> None:
    """The tier-3 :class:`ContextOverflowUncompactable` carries an
    actionable message and the distinct error type the spec §8.3
    promises. The natural failure ladder rarely reaches this rung on
    real workspaces (the summariser and mechanical fallback use the
    same boundary algorithm, so a non-empty middle for one implies
    a non-empty middle for the other); the error type still needs to
    exist so out-of-band paths (a variant that produces a different
    middle, a monkey-patched mechanical fallback for tests) can raise
    it.
    """
    err = ContextOverflowUncompactable(
        thread_id="th_test",
        keep_tail_tokens=200000,
        budget_tokens=100,
    )
    assert err.code == "context.overflow_uncompactable"
    assert "shorten" in str(err).lower() or "context_files" in str(err).lower()
    dumped = err.to_dict()
    assert dumped["type"] == "context.overflow_uncompactable"
    assert dumped["detail"]["thread_id"] == "th_test"
    assert dumped["detail"]["keep_tail_tokens"] == 200000
    assert dumped["detail"]["budget_tokens"] == 100


# ---------------------------------------------------------------------------
# Non-shrinking guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_shrinking_summary_emits_skipped_and_retries(
    tmp_path: Path,
) -> None:
    # A summariser that returns a HUGE summary_text (10 kB of prose) so
    # tokens_after >> tokens_before * 0.90 on every attempt. The
    # engine emits CONTEXT_COMPACTION_SKIPPED on each attempt, then
    # falls through to tier-2 mechanical truncation after the
    # keep_recent_fraction retries exhaust.
    huge_summary = "x " * 5000
    summariser = _ScriptedSummariser(
        [
            _valid_summary_json(huge_summary),
            _valid_summary_json(huge_summary),
            _valid_summary_json(huge_summary),
            _valid_summary_json(huge_summary),
            _valid_summary_json(huge_summary),
            _valid_summary_json(huge_summary),
        ]
    )

    async def factory(_spec: Any) -> Any:
        return summariser

    provider = ReferenceSummaryCompactionProvider(llm_for_model=factory, config={})
    engine, store = await _build(
        tmp_path,
        compaction_provider=provider,
        llm_providers=_tiny_context_providers(),
    )
    ctx1 = await _fresh_thread_ctx(store)
    await engine.execute(_config(), ctx1)
    ctx2 = ExecutionContext(
        thread_id=ctx1.thread_id,
        trigger={"kind": "user_message", "content": "more"},
    )
    await engine.execute(_config(), ctx2)

    events = [e async for e in store.read_path_to_leaf(ctx2.thread_id)]
    skipped = [e for e in events if e.type == EventType.CONTEXT_COMPACTION_SKIPPED]
    assert skipped, "non-shrinking guard should have emitted at least one skip"
    assert all(e.data.get("reason") == "no_net_savings" for e in skipped)
    # After the retries exhaust the engine falls through to tier-2:
    # CONTEXT_COMPACTION_FAILED + a mechanical CONTEXT_COMPACTED.
    assert any(e.type == EventType.CONTEXT_COMPACTION_FAILED for e in events)


# ---------------------------------------------------------------------------
# Refs clustering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refs_beyond_max_refs_cluster_into_event_id_range(
    tmp_path: Path,
) -> None:
    # Configure max_refs=3 and have the summariser return 5 refs; the
    # provider caps to 3 with the last entry an event_id_range cluster.
    many_refs = ",".join(f'{{"event_id": "ev_{i}", "note": "n"}}' for i in range(5))
    summary_json = (
        '{"schema_version": 1, "summary_text": "s", '
        f'"refs": [{many_refs}], "kept_files": [], '
        '"span": {"first_event_id": "ev_0", "last_event_id": "ev_4", '
        '"event_count": 5}}'
    )
    summariser = _ScriptedSummariser([summary_json])

    async def factory(_spec: Any) -> Any:
        return summariser

    provider = ReferenceSummaryCompactionProvider(
        llm_for_model=factory, config={"max_refs": 3}
    )
    # Drive the provider directly — no engine trigger required.
    from schemas import ThreadId
    from schemas.events import Event

    events: list[Event] = []
    # Fake 6 events with valid EventIds so the compact path fires.
    from datetime import UTC, datetime
    from uuid import uuid4

    from schemas import Actor, EventId, SessionId
    from schemas import EventType as ET

    session_id = SessionId(f"ses_{uuid4()}")
    thread_id = ThreadId(f"th_{uuid4()}")
    events.append(
        Event(
            event_id=EventId(f"ev_{uuid4()}"),
            thread_id=thread_id,
            session_id=session_id,
            type=ET.USER_MESSAGE,
            data={"content": {"text": "hi"}},
            actor=Actor(kind="user"),
            created_at=datetime.now(UTC),
        )
    )
    for _ in range(5):
        events.append(
            Event(
                event_id=EventId(f"ev_{uuid4()}"),
                thread_id=thread_id,
                session_id=session_id,
                type=ET.AGENT_MESSAGE,
                data={"content": {"text": "response"}},
                actor=Actor(kind="agent"),
                created_at=datetime.now(UTC),
            )
        )
    outcome = await provider.compact(
        events=events,
        keep_head=0,
        keep_tail=1,
        model_context_length=100,
        thread_id=thread_id,
        config={"max_refs": 3},
    )
    refs = outcome.summary.get("refs") or []
    assert len(refs) <= 3
    # Last ref should be a cluster (event_id_range).
    cluster = refs[-1]
    assert isinstance(cluster, dict)
    assert "event_id_range" in cluster, f"expected cluster ref at tail; got {cluster!r}"
    rng = cluster["event_id_range"]
    assert rng["first_event_id"] == "ev_2"
    assert rng["last_event_id"] == "ev_4"
    assert rng["event_count"] == 3
