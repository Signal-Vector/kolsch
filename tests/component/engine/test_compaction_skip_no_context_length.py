"""qol-092 — compaction skip-reason visibility (``no_context_length``).

Covers the engine-side emit at ``_maybe_compact_thread``'s silent-skip
site (``src/engine/engine.py:2446-2452``):

- The skip site emits ``CONTEXT_COMPACTION_SKIPPED`` with
  ``reason="no_context_length"`` and a ``detail`` dict carrying the
  model spec + operator-facing remediation hint, before returning.
- The remediation hint reflects the workspace's metadata-source
  availability (mirrors qol-090's engine-computed direction pattern).
- The schema stays backwards-compatible: the pre-existing
  ``no_net_savings`` emit shape doesn't populate ``detail`` and still
  round-trips.

feat-054 promised the diagnostic at spec time; the emit never landed
until qol-092. qol-091's cross-provider fallback resolves most of the
cases the old silent-skip masked, but opencode-only workspaces still
hit ``context_length is None`` — that's the case this qol makes
visible.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import yaml

import llm.providers  # noqa: F401 — force connector registrations at test import
from assets import AssetManagement
from bus import InProcessEventBus
from engine import (
    AgentEngine,
    ExecutionContext,
    LLMRequest,
    LLMResponse,
    MinimalFramework,
)
from engine.engine import _no_context_length_remediation
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

# ---------------------------------------------------------------------------
# Fixture scaffolding — mirrors tests/component/test_context_compaction_failure_ladder.py
# ---------------------------------------------------------------------------


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
                "file_access": {"allowed_paths": ["data/"], "denied_paths": [".env"]},
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
    llm_providers: dict[str, ProviderConfig],
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

    async def summariser_factory(_spec: Any) -> Any:
        return default_llm

    compaction_provider = ReferenceSummaryCompactionProvider(
        llm_for_model=summariser_factory, config={}
    )

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
        model=ModelSpec(provider="opencode", model="kimi-k2.6"),
        prompt="You are a test agent.",
        io=IOSpec(mode="conversational"),
    )


async def _fresh_ctx(store: SessionStore) -> ExecutionContext:
    session = await store.create_session("ag_test", name="test-session")
    threads = await store.list_threads(session.session_id)
    return ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": "hello"},
    )


def _opencode_no_ctx_len() -> dict[str, ProviderConfig]:
    """opencode connected, no context_length declared — the qol-092 case."""
    return {
        "opencode": ProviderConfig(
            name="opencode",
            type="opencode",
            api_key_secret=None,
            base_url=None,
            timeout_seconds=None,
            extras={},
            models=[{"id": "kimi-k2.6"}],  # no context_length
        ),
    }


# ---------------------------------------------------------------------------
# Test 1 — skip site emits the event with the expected payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emits_event_with_model_detail(tmp_path: Path) -> None:
    """qol-092 AC-1: skip site emits CONTEXT_COMPACTION_SKIPPED w/ detail."""
    engine, store = await _build(tmp_path, llm_providers=_opencode_no_ctx_len())
    ctx = await _fresh_ctx(store)
    await engine.execute(_config(), ctx)

    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    skipped = [
        e
        for e in events
        if e.type == EventType.CONTEXT_COMPACTION_SKIPPED
        and e.data.get("reason") == "no_context_length"
    ]
    assert skipped, (
        "expected a CONTEXT_COMPACTION_SKIPPED(no_context_length) event; "
        f"got reasons: {[e.data.get('reason') for e in events if e.type == EventType.CONTEXT_COMPACTION_SKIPPED]}"
    )
    event = skipped[0]
    assert event.actor.kind == "system"
    detail = event.data.get("detail") or {}
    assert isinstance(detail, dict), (
        f"detail must be a dict; got {type(detail).__name__}"
    )
    assert detail.get("provider") == "opencode"
    assert detail.get("model") == "kimi-k2.6"
    hint = detail.get("remediation_hint")
    assert isinstance(hint, str) and hint, "remediation_hint must be a non-empty string"


# ---------------------------------------------------------------------------
# Test 4 (part) — remediation hint reflects workspace state
# ---------------------------------------------------------------------------


def test_remediation_hint_no_metadata_source() -> None:
    """Without a metadata source, the hint recommends connect/edit."""
    providers = _opencode_no_ctx_len()
    spec = ModelSpec(provider="opencode", model="kimi-k2.6")
    hint = _no_context_length_remediation(providers, spec)
    assert "kolsch connect openrouter" in hint or "platform.yaml" in hint
    assert "kolsch refresh" not in hint


def test_remediation_hint_with_metadata_source() -> None:
    """With OpenRouter connected as a metadata source, hint recommends refresh."""
    providers: dict[str, ProviderConfig] = {
        "opencode": ProviderConfig(
            name="opencode",
            type="opencode",
            api_key_secret=None,
            base_url=None,
            timeout_seconds=None,
            extras={},
            models=[{"id": "kimi-k2.6"}],
        ),
        "openrouter": ProviderConfig(
            name="openrouter",
            type="openrouter",
            api_key_secret=None,
            base_url=None,
            timeout_seconds=None,
            extras={},
            models=[],
        ),
    }
    spec = ModelSpec(provider="opencode", model="kimi-k2.6")
    hint = _no_context_length_remediation(providers, spec)
    assert "kolsch refresh opencode" in hint


def test_remediation_hint_empty_providers_is_safe() -> None:
    """No providers at all — helper must still return a usable string."""
    spec = ModelSpec(provider="opencode", model="kimi-k2.6")
    hint = _no_context_length_remediation({}, spec)
    assert isinstance(hint, str) and hint


def test_remediation_hint_none_providers_is_safe() -> None:
    """``providers is None`` — helper must return a usable string, not crash."""
    spec = ModelSpec(provider="opencode", model="kimi-k2.6")
    hint = _no_context_length_remediation(None, spec)
    assert isinstance(hint, str) and hint


# ---------------------------------------------------------------------------
# Test — schema back-compat: no_net_savings emit still round-trips w/o detail
# ---------------------------------------------------------------------------


def test_schema_backwards_compatible_no_detail() -> None:
    """qol-092 AC-4: ContextCompactionSkippedData without ``detail`` still validates."""
    from schemas.events import ContextCompactionSkippedData

    data = ContextCompactionSkippedData(
        reason="no_net_savings", tokens_before=10, tokens_after=8
    )
    assert data.reason == "no_net_savings"
    assert data.tokens_before == 10
    assert data.tokens_after == 8
    assert data.detail is None


def test_schema_accepts_detail_dict() -> None:
    """qol-092: the new ``detail`` field accepts an optional string→string map."""
    from schemas.events import ContextCompactionSkippedData

    data = ContextCompactionSkippedData(
        reason="no_context_length",
        detail={
            "provider": "opencode",
            "model": "kimi-k2.6",
            "remediation_hint": "Connect a metadata source ...",
        },
    )
    assert data.detail is not None
    assert data.detail["provider"] == "opencode"


# ---------------------------------------------------------------------------
# bug-097 — per-thread idempotency guard on the ``no_context_length`` emit
# ---------------------------------------------------------------------------


def _ctx_for_thread(thread_id: Any, message: str) -> ExecutionContext:
    """Build an ExecutionContext for a specific thread + trigger message."""
    return ExecutionContext(
        thread_id=thread_id,
        trigger={"kind": "user_message", "content": message},
    )


@pytest.mark.asyncio
async def test_skip_emit_is_idempotent_across_turns(tmp_path: Path) -> None:
    """bug-097 AC-1: a wedged multi-turn session emits ONE skip event, not N.

    The debug log still fires per turn (engine-side observability), but the
    chat-visible emit is thread-scoped so the transcript doesn't stack N
    duplicate ``CompactionBlock`` widgets.
    """
    engine, store = await _build(tmp_path, llm_providers=_opencode_no_ctx_len())
    ctx = await _fresh_ctx(store)
    thread_id = ctx.thread_id

    # Simulate five user turns on the same thread. Each ``execute`` call
    # runs one exchange → one ``_maybe_compact_events`` call → potential
    # skip emit. Post-fix: only the first exchange emits.
    for i in range(5):
        await engine.execute(_config(), _ctx_for_thread(thread_id, f"msg-{i}"))

    events = [e async for e in store.read_path_to_leaf(thread_id)]
    skipped = [
        e
        for e in events
        if e.type == EventType.CONTEXT_COMPACTION_SKIPPED
        and e.data.get("reason") == "no_context_length"
    ]
    assert len(skipped) == 1, (
        f"expected exactly one no_context_length skip event across 5 turns; "
        f"got {len(skipped)}"
    )
    # AC-4 completeness — confirm the guard didn't silently drop
    # subsequent turns. Every ``execute`` call should have appended
    # its user message + at least one exchange terminal, so the JSONL
    # carries evidence that all 5 turns actually ran.
    user_messages = [e for e in events if e.type == EventType.USER_MESSAGE]
    assert len(user_messages) == 5, (
        f"expected 5 user messages across 5 turns (guard must not "
        f"abort the exchange); got {len(user_messages)}"
    )


@pytest.mark.asyncio
async def test_guard_is_thread_scoped(tmp_path: Path) -> None:
    """bug-097 AC-3: two threads → each gets its own first-turn emit.

    Rules out a process-scoped guard that would incorrectly silence the
    second thread's first turn.
    """
    engine, store = await _build(tmp_path, llm_providers=_opencode_no_ctx_len())
    ctx_a = await _fresh_ctx(store)
    ctx_b = await _fresh_ctx(store)
    assert ctx_a.thread_id != ctx_b.thread_id

    await engine.execute(_config(), ctx_a)
    await engine.execute(_config(), ctx_b)

    for thread_id in (ctx_a.thread_id, ctx_b.thread_id):
        events = [e async for e in store.read_path_to_leaf(thread_id)]
        skipped = [
            e
            for e in events
            if e.type == EventType.CONTEXT_COMPACTION_SKIPPED
            and e.data.get("reason") == "no_context_length"
        ]
        assert len(skipped) == 1, (
            f"thread {thread_id} expected exactly one skip event; got {len(skipped)}"
        )
