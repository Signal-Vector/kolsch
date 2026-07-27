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
async def test_manual_compact_emits_no_context_length(tmp_path: Path) -> None:
    """bug-098 AC-1: ``/compact`` (via manual_compact) emits distinct event on wedged workspace.

    Before bug-098 the sibling ``_manual_compact_locked`` silent-returned
    None on ``context_length is None`` — the chat surfaced only the
    opaque three-way notice at ``app.py:2292-2296`` with no remediation
    hint. After: emits ``CONTEXT_COMPACTION_SKIPPED(no_context_length)``
    with the same payload shape as the per-turn (qol-092) emit and
    returns the event, which routes the slash handler into the
    "adapter mounts the distinct notice" branch.
    """
    engine, store = await _build(tmp_path, llm_providers=_opencode_no_ctx_len())
    ctx = await _fresh_ctx(store)

    emitted = await engine.manual_compact(config=_config(), thread_id=ctx.thread_id)

    assert emitted is not None, (
        "manual_compact must return the emitted event so the slash handler "
        "skips the opaque three-way notice"
    )
    assert emitted.type == EventType.CONTEXT_COMPACTION_SKIPPED
    assert emitted.data.get("reason") == "no_context_length"
    detail = emitted.data.get("detail") or {}
    assert detail.get("provider") == "opencode"
    assert detail.get("model") == "kimi-k2.6"
    assert (
        isinstance(detail.get("remediation_hint"), str) and detail["remediation_hint"]
    )

    # And the JSONL stored the event.
    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    skipped = [
        e
        for e in events
        if e.type == EventType.CONTEXT_COMPACTION_SKIPPED
        and e.data.get("reason") == "no_context_length"
    ]
    assert len(skipped) == 1


@pytest.mark.asyncio
async def test_eager_compaction_emits_no_context_length(tmp_path: Path) -> None:
    """bug-098 AC-2: session-open eager path emits distinct event on wedged workspace.

    Before bug-098 the eager path silent-returned None; a resumed
    wedged session opened with zero warning and ran to the LLM
    ceiling. After: eager emits the distinct event so the chat surface
    warns on resume — but ONLY when the thread has history at risk
    (bug-098 review F2). Populate the thread via a per-turn exchange
    first to establish the "resume-with-history" precondition, then
    clear the guard so eager gets a fresh chance to emit.
    """
    engine, store = await _build(tmp_path, llm_providers=_opencode_no_ctx_len())
    ctx = await _fresh_ctx(store)
    spec = ModelSpec(provider="opencode", model="kimi-k2.6")

    # Populate thread history; per-turn will emit + populate guard.
    await engine.execute(_config(), ctx)
    # Clear the guard so eager gets a chance to emit on this call —
    # simulates a fresh process resume where the in-memory guard is
    # empty but the on-disk thread carries prior events.
    engine._no_context_length_skip_emitted.clear()

    result = await engine.maybe_compact_thread_eagerly(
        thread_id=ctx.thread_id,
        spec=spec,
    )

    assert result is not None, "eager path must return the emitted skip event"
    assert result.type == EventType.CONTEXT_COMPACTION_SKIPPED
    assert result.data.get("reason") == "no_context_length"


@pytest.mark.asyncio
async def test_eager_healthy_provider_avoids_full_log_read(tmp_path: Path) -> None:
    """qol-100 AC-1: healthy providers don't pay a full ``read_path_to_leaf``
    at chat-open.

    bug-098's fold-in moved ``read_path_to_leaf`` + ``any(...)`` BEFORE
    the ``context_length`` check, which fixed the fresh-session-noise
    problem but made healthy providers pay a full-log read for a
    boolean. qol-100 replaces that with the cheap
    ``has_any_event_of_type`` probe. This test spies on both to
    verify the cheap path is taken and the expensive path is not
    (until it's genuinely needed downstream).
    """
    # Healthy provider: OpenRouter-shaped entry with context_length declared.
    from llm import ProviderConfig

    providers: dict[str, ProviderConfig] = {
        "openrouter": ProviderConfig(
            name="openrouter",
            type="openrouter",
            api_key_secret=None,
            base_url=None,
            timeout_seconds=None,
            extras={},
            models=[
                {"id": "moonshotai/kimi-k2.6", "context_length": 262144},
            ],
        ),
    }
    engine, store = await _build(tmp_path, llm_providers=providers)
    ctx = await _fresh_ctx(store)
    thread_id = ctx.thread_id
    spec = ModelSpec(provider="openrouter", model="moonshotai/kimi-k2.6")

    # Fresh thread — no USER_MESSAGE yet.
    read_path_calls = 0
    has_probe_calls = 0

    original_read_path = store.read_path_to_leaf
    original_has_probe = store.has_any_event_of_type

    def _count_read_path(*args: Any, **kwargs: Any) -> Any:
        nonlocal read_path_calls
        read_path_calls += 1
        return original_read_path(*args, **kwargs)

    async def _count_probe(*args: Any, **kwargs: Any) -> bool:
        nonlocal has_probe_calls
        has_probe_calls += 1
        return await original_has_probe(*args, **kwargs)

    store.read_path_to_leaf = _count_read_path  # type: ignore[method-assign]
    store.has_any_event_of_type = _count_probe  # type: ignore[method-assign]
    try:
        await engine.maybe_compact_thread_eagerly(thread_id=thread_id, spec=spec)
    finally:
        store.read_path_to_leaf = original_read_path  # type: ignore[method-assign]
        store.has_any_event_of_type = original_has_probe  # type: ignore[method-assign]

    assert has_probe_calls == 1, (
        f"eager should call has_any_event_of_type exactly once; got {has_probe_calls}"
    )
    assert read_path_calls == 0, (
        f"eager on a fresh session must NOT call read_path_to_leaf — "
        f"the cheap probe should have short-circuited; got {read_path_calls} calls"
    )


@pytest.mark.asyncio
async def test_eager_on_fresh_empty_session_stays_silent(tmp_path: Path) -> None:
    """bug-098 review F2: eager on empty session must NOT emit.

    Fresh chat launch on a wedged provider used to trip the skip emit
    at chat-open time before the user even sent a message — noise,
    since there's no history at risk yet. The fix reorders the checks
    so eager consults the thread's event stream first and no-ops
    silently on empty history.
    """
    engine, store = await _build(tmp_path, llm_providers=_opencode_no_ctx_len())
    ctx = await _fresh_ctx(store)
    spec = ModelSpec(provider="opencode", model="kimi-k2.6")

    # Thread is empty (just the fresh context — no execute has run).
    result = await engine.maybe_compact_thread_eagerly(
        thread_id=ctx.thread_id,
        spec=spec,
    )

    assert result is None, (
        "eager on an empty session must silently no-op — a wedged provider "
        "notice at chat-launch with no history is noise"
    )
    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    skipped = [
        e
        for e in events
        if e.type == EventType.CONTEXT_COMPACTION_SKIPPED
        and e.data.get("reason") == "no_context_length"
    ]
    assert not skipped, (
        f"eager on empty session must not append a skip event; got {len(skipped)}"
    )


@pytest.mark.asyncio
async def test_shared_guard_suppresses_eager_after_per_turn_emit(
    tmp_path: Path,
) -> None:
    """bug-098 AC-3: eager path shares bug-097's per-thread guard.

    Sequence: per-turn ``execute`` emits (populates guard). Then a
    later eager call on the same thread finds the guard set and
    returns None (no duplicate notice on session resume).

    ``/compact`` (``_manual_compact_locked``) deliberately does NOT
    share the guard — user-initiated actions get fresh responses each
    time (a new pending CompactionBlock is mounted by the slash
    handler and consumed via ``_acquire_compaction_block``).
    """
    engine, store = await _build(tmp_path, llm_providers=_opencode_no_ctx_len())
    ctx = await _fresh_ctx(store)
    spec = ModelSpec(provider="opencode", model="kimi-k2.6")

    # Turn 1 via per-turn path — populates the guard.
    await engine.execute(_config(), ctx)
    # Now eager is called (e.g. simulated session resume) — should be
    # suppressed by the shared guard.
    eager_result = await engine.maybe_compact_thread_eagerly(
        thread_id=ctx.thread_id,
        spec=spec,
    )
    assert eager_result is None, (
        "eager path must respect the per-thread guard set by the per-turn emit"
    )

    # /compact by contrast IS user-initiated — still emits.
    manual_result = await engine.manual_compact(
        config=_config(), thread_id=ctx.thread_id
    )
    assert manual_result is not None
    assert manual_result.type == EventType.CONTEXT_COMPACTION_SKIPPED
    assert manual_result.data.get("reason") == "no_context_length"

    # Cross-check: JSONL now has TWO events (per-turn's first emit +
    # /compact's fresh emit), zero from eager (guard suppressed).
    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    skipped = [
        e
        for e in events
        if e.type == EventType.CONTEXT_COMPACTION_SKIPPED
        and e.data.get("reason") == "no_context_length"
    ]
    assert len(skipped) == 2, (
        f"expected 2 no_context_length events (per-turn + /compact); got {len(skipped)}"
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
