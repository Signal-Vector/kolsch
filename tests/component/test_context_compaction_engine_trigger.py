"""feat-054 Phase 2 — engine trigger + compaction pre-flight.

Covers:

- Provider ``None``: no compaction, no emit.
- Provider present + context_length ``None``: no compaction, no emit.
- Provider present + estimate above threshold: emit
  ``context.compacted``.
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
from engine.assembly import COMPACTION_MESSAGE_TAG
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
from tests.fixtures.assertions import assert_event_content, assert_event_sequence
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


async def _fresh_thread_ctx(
    store: SessionStore, agent_id: str = "ag_test"
) -> ExecutionContext:
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

    async def factory(_spec: Any, *, context: Any = None) -> Any:
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

    async def factory(_spec: Any, *, context: Any = None) -> Any:
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
    assert any(e.type == EventType.CONTEXT_COMPACTED for e in events), [
        e.type.value for e in events
    ]


# ---------------------------------------------------------------------------
# bug-156 — the compaction summary must reach the model on the turn it
# fires. The per-exchange pre-flight emits `context.compacted` AFTER the
# trigger `user.message`, so Assembly projects the summary after the user
# message and the engine's history split used to slice it off.
# ---------------------------------------------------------------------------

_FIRST_Q = "first question about the deployment plan"
_MIDDLE_Q = "middle question " + ("about the rollout window and its risks " * 52)
_THIRD_Q = "third question"
_LONG_REPLY = "answer " + ("with enough detail to carry token weight " * 25)
_SUMMARY_JSON = (
    '{"schema_version": 1, "summary_text": "earlier turns covered rollout", '
    '"refs": [], "kept_files": [], '
    '"span": {"first_event_id": "ev_x", "last_event_id": "ev_y", '
    '"event_count": 2}}'
)
_LARGE_CONTEXT = 100_000


class _RecordingLLM:
    """Main-LLM callable that records every request it receives."""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(content=_LONG_REPLY, tool_calls=None, usage={}, raw={})


def _summariser_factory(*, fail: bool) -> Any:
    class _Handle:
        async def __call__(self, request: LLMRequest) -> LLMResponse:
            if fail:
                raise RuntimeError("summariser unavailable")
            return LLMResponse(content=_SUMMARY_JSON, tool_calls=None, usage={}, raw={})

    async def factory(_spec: Any, *, context: Any = None) -> Any:
        return _Handle()

    return factory


def _mock_provider(context_length: int) -> ProviderConfig:
    return ProviderConfig(
        name="mock",
        type="mock",
        api_key_secret=None,
        base_url=None,
        timeout_seconds=None,
        extras={},
        models=[{"id": "mock-model", "context_length": context_length}],
    )


def _set_context_length(llm_providers: dict[str, ProviderConfig], value: int) -> None:
    llm_providers["mock"].models[0]["context_length"] = value


async def _build_recording(
    tmp_path: Path, *, fail_summariser: bool = False
) -> tuple[AgentEngine, SessionStore, _RecordingLLM, dict[str, ProviderConfig]]:
    llm = _RecordingLLM()
    llm_providers = {"mock": _mock_provider(_LARGE_CONTEXT)}
    provider = ReferenceSummaryCompactionProvider(
        llm_for_model=_summariser_factory(fail=fail_summariser),
        config={},
    )
    engine, store = await _build(
        tmp_path,
        compaction_provider=provider,
        llm_providers=llm_providers,
        llm=llm,
    )
    return engine, store, llm, llm_providers


async def _run_turn(engine: AgentEngine, thread_id: Any, content: str) -> None:
    await engine.execute(
        _config(),
        ExecutionContext(
            thread_id=thread_id,
            trigger={"kind": "user_message", "content": content},
        ),
    )


async def _seed_three_exchanges(engine: AgentEngine, store: SessionStore) -> Any:
    """Exchanges 1-3 at a large context_length (no compaction)."""
    session = await store.create_session("ag_test", name="test-session")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id
    await _run_turn(engine, thread_id, _FIRST_Q)
    await _run_turn(engine, thread_id, _MIDDLE_Q)
    await _run_turn(engine, thread_id, _THIRD_Q)
    return thread_id


def _role_content(messages: list[Any]) -> list[tuple[str, str]]:
    """(role, content) pairs from Message models or their JSON dumps."""
    pairs: list[tuple[str, str]] = []
    for m in messages:
        if isinstance(m, dict):
            pairs.append((m["role"], m.get("content") or ""))
        else:
            pairs.append((m.role, m.content or ""))
    return pairs


def _assert_summary_right_before_user(
    messages: list[Any], *, user_text: str, absent: str | None = None
) -> None:
    pairs = _role_content(messages)
    shown = [(role, content[:40]) for role, content in pairs]
    summaries = [
        i
        for i, (role, content) in enumerate(pairs)
        if role == "system" and content.startswith(COMPACTION_MESSAGE_TAG)
    ]
    assert len(summaries) == 1, f"expected exactly one compaction summary: {shown}"
    assert pairs[-1] == ("user", user_text), shown
    assert summaries[0] == len(pairs) - 2, (
        f"summary not immediately before the user message: {shown}"
    )
    if absent is not None:
        assert not any(absent in content for _, content in pairs), shown


async def _events_since_last_exchange_start(
    store: SessionStore, thread_id: Any
) -> list[dict[str, Any]]:
    events = [
        e.model_dump(mode="json") async for e in store.read_path_to_leaf(thread_id)
    ]
    start = max(
        i for i, e in enumerate(events) if e["type"] == "thread.exchange_started"
    )
    return events[start:]


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["auto", "auto_truncation"])
async def test_per_exchange_compaction_summary_reaches_framework(
    tmp_path: Path, trigger: str
) -> None:
    engine, store, llm, llm_providers = await _build_recording(
        tmp_path, fail_summariser=trigger == "auto_truncation"
    )
    session = await store.create_session("ag_test", name="test-session")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id
    await _run_turn(engine, thread_id, _FIRST_Q)
    await _run_turn(engine, thread_id, _MIDDLE_Q)

    # Shrink the window so the exchange-3 pre-flight fires.
    _set_context_length(llm_providers, 4700)
    await _run_turn(engine, thread_id, _THIRD_Q)

    exchange = await _events_since_last_exchange_start(store, thread_id)
    ordered = [
        e
        for e in exchange
        if e["type"] in ("user.message", "context.compacted", "model.request")
    ]
    assert_event_sequence(
        ordered, ["user.message", "context.compacted", "model.request"]
    )
    assert_event_content(ordered[1], trigger=trigger)
    assert_event_content(ordered[0], content={"text": _THIRD_Q})

    # (a) what the main LLM actually received on exchange 3.
    assert len(llm.requests) == 3
    _assert_summary_right_before_user(
        llm.requests[-1].messages, user_text=_THIRD_Q, absent="middle question"
    )
    # (b) what the log recorded on `model.request`.
    _assert_summary_right_before_user(
        ordered[2]["data"]["messages"], user_text=_THIRD_Q, absent="middle question"
    )

    # Next turn, no new compaction: the summary is still delivered exactly
    # once, at its projected position between "third question" and its
    # reply (Assembly placement) — not moved to the end again.
    _set_context_length(llm_providers, _LARGE_CONTEXT)
    await _run_turn(engine, thread_id, "fourth question")
    pairs = _role_content(llm.requests[-1].messages)
    shown = [(role, content[:40]) for role, content in pairs]
    summaries = [
        i
        for i, (role, content) in enumerate(pairs)
        if role == "system" and content.startswith(COMPACTION_MESSAGE_TAG)
    ]
    assert len(summaries) == 1, shown
    idx = summaries[0]
    assert pairs[idx - 1] == ("user", _THIRD_Q), shown
    assert pairs[idx + 1] == ("assistant", _LONG_REPLY), shown
    assert pairs[-1] == ("user", "fourth question"), shown


@pytest.mark.asyncio
async def test_manual_compact_summary_reaches_next_exchange(tmp_path: Path) -> None:
    engine, store, llm, llm_providers = await _build_recording(tmp_path)
    thread_id = await _seed_three_exchanges(engine, store)

    # A small window makes the kept recent tail short enough that the
    # middle span is non-empty (otherwise `manual_compact` returns None).
    _set_context_length(llm_providers, 4700)
    event = await engine.manual_compact(config=_config(), thread_id=thread_id)
    assert event is not None
    assert event.type == EventType.CONTEXT_COMPACTED

    _set_context_length(llm_providers, _LARGE_CONTEXT)
    await _run_turn(engine, thread_id, "fourth question")
    _assert_summary_right_before_user(
        llm.requests[-1].messages, user_text="fourth question"
    )


@pytest.mark.asyncio
async def test_eager_compact_summary_reaches_next_exchange(tmp_path: Path) -> None:
    engine, store, llm, llm_providers = await _build_recording(tmp_path)
    thread_id = await _seed_three_exchanges(engine, store)

    _set_context_length(llm_providers, 4300)
    event = await engine.maybe_compact_thread_eagerly(
        thread_id=thread_id, spec=_config().model
    )
    assert event is not None
    assert event.type == EventType.CONTEXT_COMPACTED

    _set_context_length(llm_providers, _LARGE_CONTEXT)
    await _run_turn(engine, thread_id, "fourth question")
    _assert_summary_right_before_user(
        llm.requests[-1].messages, user_text="fourth question"
    )
