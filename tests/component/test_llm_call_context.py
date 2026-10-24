"""bug-144 Test 1 — the `LLMCallContext` reaches the connector and the handle.

A recording fake connector is registered in the LLM registry and the
REAL engine + `make_build_llm_factory` + `llm.build_llm` chain is used
(no network, no tokens — the fake's `crewai.LLM` stand-in returns canned
text). Asserts:

(a) every model call's `context.session_id` / `conversation_key` equal
    the run's session id across a two-turn conversation;
(b) a fallback chain (primary raises Recoverable `model.server_error`)
    hands the SAME context object to both attempts;
(c) two sessions record two different keys (the `/new` case);
(d) the compaction summariser call (manual `/compact` and session-open
    eager) records its session's id;
(e) `FallbackLLMHandle.context` is the object the factory received.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import yaml

from assets import AssetManagement
from bus import InProcessEventBus
from engine import AgentEngine, ExecutionContext, MinimalFramework
from engine._crewai_adapter import make_build_llm_factory
from errors import ModelServerError
from files import PathPolicy
from llm import LLMCallContext, ProviderConfig, registry
from providers import (
    NativeSandboxProvider,
    RecentBufferMemoryProvider,
    RuleBasedAuthorizationProvider,
)
from providers.compaction import ReferenceSummaryCompactionProvider
from providers.secrets import DotenvSecretsProvider
from schemas import AgentId, EventType, IOSpec, ModelSpec, ResolvedAgentConfig
from skills import SkillsSubsystem
from store import SessionStore, SqliteBackend
from tools import ToolsSubsystem

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


_TYPE = "ctxrec_bug144"
_PROVIDER = "ctxrec"
_SUMMARY_JSON = (
    '{"schema_version": 1, "summary_text": "s", "refs": [], '
    '"kept_files": [], "span": {"first_event_id": "e", '
    '"last_event_id": "f", "event_count": 1}}'
)


class _Recorded:
    """One `build_llm` invocation the fake connector saw."""

    def __init__(self, model_id: str, context: LLMCallContext | None) -> None:
        self.model_id = model_id
        self.context = context


_RECORDS: list[_Recorded] = []


class _FakeLLM:
    """Stands in for `crewai.LLM` — only `.call` is used by the adapter."""

    def __init__(self, model_id: str) -> None:
        self.model = model_id

    def call(self, messages: Any, tools: Any = None) -> str:
        if self.model == "flaky":
            raise ModelServerError(provider=_PROVIDER, model="flaky", status_code=503)
        if self.model == "summ":
            return _SUMMARY_JSON
        return "ok"


class _RecordingConnector:
    type_name = _TYPE

    def auth_prompt_spec(self) -> Any:  # pragma: no cover — unused
        raise NotImplementedError

    async def validate_credentials(
        self, credentials: Any, base_url: str | None
    ) -> Any:  # pragma: no cover — unused
        raise NotImplementedError

    async def discover_models(
        self, credentials: Any, base_url: str | None
    ) -> Any:  # pragma: no cover — unused
        raise NotImplementedError

    def classify_call_error(
        self, exc: BaseException, *, provider_config: Any, model_id: str
    ) -> Any:  # pragma: no cover — unused (bug-145 protocol)
        return None

    async def build_llm(
        self,
        model_id: str,
        provider_config: ProviderConfig,
        api_key: str | None,
        parameters: Any = None,
        *,
        context: LLMCallContext | None = None,
    ) -> Any:
        _RECORDS.append(_Recorded(model_id, context))
        return _FakeLLM(model_id)


@pytest.fixture(autouse=True)
def _registered() -> Iterator[None]:
    _RECORDS.clear()
    registry.register(_TYPE, _RecordingConnector)
    try:
        yield
    finally:
        registry._REGISTRY.pop(_TYPE, None)
        _RECORDS.clear()


def _platform_yaml(root: Path, *, compaction: bool) -> None:
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (root / sub).mkdir(exist_ok=True)
    body: dict[str, Any] = {
        "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
        "sandbox": {"variant": "native", "config": {}},
        "authorization": {
            "variant": "rule_based",
            "config": {"rules": [{"who": ".*", "what": ".*", "effect": "allow"}]},
        },
        "storage": {"entity_backend": "sqlite", "entity_path": ".platform/entities.db"},
        "event_bus": {"backend": "in_process"},
        "file_access": {"allowed_paths": ["data/"], "denied_paths": [".env"]},
        "defaults": {},
    }
    if compaction:
        body["compaction"] = {
            "variant": "reference_summary",
            "config": {"summarizer_model": {"provider": _PROVIDER, "model": "summ"}},
        }
    (root / "platform.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    (root / ".env").write_text("", encoding="utf-8")


async def _build(
    tmp_path: Path,
    *,
    context_length: int = 1_000_000,
    compaction: bool = False,
    wire_factory: bool = True,
) -> tuple[AgentEngine, SessionStore]:
    _platform_yaml(tmp_path, compaction=compaction)
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
    providers_cfg = {
        _PROVIDER: ProviderConfig(
            name=_PROVIDER,
            type=_TYPE,
            api_key_secret=None,
            base_url=None,
            timeout_seconds=None,
            extras={},
            models=[
                {"id": m, "context_length": context_length}
                for m in ("steady", "flaky", "summ")
            ],
        ),
    }
    factory = make_build_llm_factory(
        llm_providers_config=providers_cfg, secrets=secrets
    )
    engine = AgentEngine(
        store=store,
        bus=InProcessEventBus(),
        asset_management=am,
        tools_subsystem=ToolsSubsystem(
            secrets=secrets,
            sandbox=sandbox,
            authz=authz,
            path_policy=PathPolicy(allowed_paths=["data/"], denied_paths=[]),
            root=tmp_path,
        ),
        skills_subsystem=SkillsSubsystem(asset_management=am),
        memory_provider=RecentBufferMemoryProvider(store=store),
        secrets=secrets,
        sandbox=sandbox,
        authz=authz,
        framework=MinimalFramework(),
        llm_providers_config=providers_cfg,
        llm_for_model=factory if wire_factory else None,
        compaction_provider=(
            ReferenceSummaryCompactionProvider(llm_for_model=factory, config={})
            if compaction
            else None
        ),
    )
    return engine, store


def _config(
    model: ModelSpec, *, fallback: list[ModelSpec] | None = None
) -> ResolvedAgentConfig:
    return ResolvedAgentConfig(
        schema_version="0.1",
        assembly_version="2",
        agent_id=AgentId("ag_ctx"),
        name="ctx-agent",
        description="bug-144 context agent.",
        model=model,
        fallback_models=fallback or [],
        prompt="You are a test agent.",
        io=IOSpec(mode="conversational"),
    )


async def _new_session(store: SessionStore) -> tuple[str, Any]:
    session = await store.create_session("ag_ctx", name="bug-144")
    threads = await store.list_threads(session.session_id)
    return str(session.session_id), threads[0].thread_id


async def _turn(engine: AgentEngine, cfg: ResolvedAgentConfig, thread_id: Any) -> None:
    ctx = ExecutionContext(
        thread_id=thread_id,
        trigger={"kind": "user_message", "content": "hello " * 20},
    )
    reply = await engine.execute(cfg, ctx)
    assert reply.type == EventType.AGENT_MESSAGE


async def test_llm_call_context_matches_session_across_turns(tmp_path: Path) -> None:
    """(a) every model call in a two-turn conversation carries the session id."""
    engine, store = await _build(tmp_path)
    session_id, thread_id = await _new_session(store)
    cfg = _config(ModelSpec(provider=_PROVIDER, model="steady"))

    await _turn(engine, cfg, thread_id)
    await _turn(engine, cfg, thread_id)

    assert len(_RECORDS) == 2
    for rec in _RECORDS:
        assert rec.context is not None
        assert rec.context.session_id == session_id
        assert rec.context.conversation_key == session_id
        assert rec.context.agent_id == "ag_ctx"


async def test_llm_call_context_identical_across_fallback_attempts(
    tmp_path: Path,
) -> None:
    """(b) primary fails Recoverable → fallback gets the SAME context object."""
    engine, store = await _build(tmp_path)
    session_id, thread_id = await _new_session(store)
    cfg = _config(
        ModelSpec(provider=_PROVIDER, model="flaky"),
        fallback=[ModelSpec(provider=_PROVIDER, model="steady")],
    )

    await _turn(engine, cfg, thread_id)

    assert [r.model_id for r in _RECORDS] == ["flaky", "steady"]
    first, second = (r.context for r in _RECORDS)
    assert first is not None
    assert first is second
    assert first.conversation_key == session_id
    events = [e async for e in store.read_path_to_leaf(thread_id)]
    assert any(e.type == EventType.MODEL_FALLBACK for e in events)


async def test_llm_call_context_differs_between_sessions(tmp_path: Path) -> None:
    """(c) a second session (the `/new` case) gets a different key."""
    engine, store = await _build(tmp_path)
    cfg = _config(ModelSpec(provider=_PROVIDER, model="steady"))
    sid_a, thread_a = await _new_session(store)
    sid_b, thread_b = await _new_session(store)

    await _turn(engine, cfg, thread_a)
    await _turn(engine, cfg, thread_b)

    keys = [r.context.conversation_key for r in _RECORDS if r.context is not None]
    assert keys == [sid_a, sid_b]
    assert sid_a != sid_b


async def test_llm_call_context_reaches_compaction_summariser(tmp_path: Path) -> None:
    """(d) the summariser build (auto + manual `/compact`) carries the session id."""
    engine, store = await _build(tmp_path, context_length=200, compaction=True)
    session_id, thread_id = await _new_session(store)
    cfg = _config(ModelSpec(provider=_PROVIDER, model="steady"))
    for _ in range(4):
        await _turn(engine, cfg, thread_id)
    before = len([r for r in _RECORDS if r.model_id == "summ"])

    emitted = await engine.manual_compact(config=cfg, thread_id=thread_id)

    assert emitted is not None
    summ = [r for r in _RECORDS if r.model_id == "summ"]
    assert len(summ) > before, "manual /compact never built the summariser"
    for rec in summ:
        assert rec.context is not None
        assert rec.context.session_id == session_id
        assert rec.context.conversation_key == session_id


async def test_llm_call_context_reaches_eager_session_open_compaction(
    tmp_path: Path,
) -> None:
    """(d') session-open eager compaction carries the session id + its agent."""
    engine, store = await _build(tmp_path, context_length=200, compaction=True)
    session_id, thread_id = await _new_session(store)
    cfg = _config(ModelSpec(provider=_PROVIDER, model="steady"))
    for _ in range(4):
        await _turn(engine, cfg, thread_id)
    before = len(_RECORDS)

    emitted = await engine.maybe_compact_thread_eagerly(
        thread_id=thread_id,
        spec=cfg.model,
    )

    assert emitted is not None
    eager = [r for r in _RECORDS[before:] if r.model_id == "summ"]
    assert eager, "eager session-open compaction never built the summariser"
    for rec in eager:
        assert rec.context is not None
        assert rec.context.conversation_key == session_id
        assert rec.context.agent_id == "ag_ctx"  # the session's recorded agent


async def test_llm_call_context_on_handle_is_factory_context(tmp_path: Path) -> None:
    """(e) `FallbackLLMHandle.context` is the object the factory received."""
    from engine import _fallback

    engine, store = await _build(tmp_path)
    session_id, thread_id = await _new_session(store)
    cfg = _config(ModelSpec(provider=_PROVIDER, model="steady"))
    handles: list[Any] = []
    real_init = _fallback.FallbackLLMHandle.__init__

    def _spy_init(self: Any, **kwargs: Any) -> None:
        real_init(self, **kwargs)
        handles.append(self)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_fallback.FallbackLLMHandle, "__init__", _spy_init)
        await _turn(engine, cfg, thread_id)

    assert len(handles) == 1
    assert len(_RECORDS) == 1
    assert handles[0].context is _RECORDS[0].context
    assert handles[0].context.conversation_key == session_id


async def test_llm_call_context_reaches_preflight_build(tmp_path: Path) -> None:
    """Pre-flight `_acquire_llm_if_needed` (no `llm_for_model=`) passes the context.

    With neither `llm=` nor `llm_for_model=` the exchange cannot run a
    model call, so it fails after pre-flight — the assertion is on the
    pre-flight `build_llm` the fake connector recorded.
    """
    engine, store = await _build(tmp_path, wire_factory=False)
    session_id, thread_id = await _new_session(store)
    cfg = _config(ModelSpec(provider=_PROVIDER, model="steady"))
    ctx = ExecutionContext(
        thread_id=thread_id,
        trigger={"kind": "user_message", "content": "hello"},
    )

    with pytest.raises(Exception):  # noqa: B017 — any failure after pre-flight
        await engine.execute(cfg, ctx)

    assert [r.model_id for r in _RECORDS] == ["steady"]
    rec = _RECORDS[0]
    assert rec.context is not None
    assert rec.context.conversation_key == session_id
