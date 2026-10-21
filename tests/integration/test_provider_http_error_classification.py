"""bug-145 Test 4 — provider HTTP errors are typed at the call seam.

End to end with the REAL pieces: `make_build_llm_factory` builds each
chain entry's client through the real `OpenCodeConnector.build_llm`
(the `crewai.LLM` kolsch ships) and binds the real
`OpenCodeConnector.classify_call_error`; the real `CrewAIFramework`
and `AgentEngine` drive the exchange. Only the wire is fake: bug-144's
harness idiom swaps an `httpx.MockTransport` into the built client
(`client_params["http_client"]`, `_client = None`), so every response
is canned and no token is spent.

**SDK retries are disabled** by setting `max_retries = 0` on the built
client before it is first used (CrewAI forwards it to the `openai`
client via `_get_client_params`), so 429 / 503 return immediately
instead of sleeping through the SDK's two retries.

Pinned (AC 7 / AC 8):

- the ses_0cd8df9d incident — OpenCode `400 MissingSessionID` — lands
  as `model.bad_request` on `model.response` and
  `thread.exchange_failed`, not `platform.internal_error` at
  `engine.execute.boundary`, and never falls back;
- 429 / 503 / 404 on the primary advance a one-entry fallback chain
  and the exchange completes on the fallback;
- 400 / 401 with a chain configured do not fall back;
- a 404 with NO chain is Recoverable, so the engine's retry loop
  re-sends it `max_retries + 1` times (QA NB-3: cheap — no inference —
  but asserted so the behaviour is explicit).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

import httpx
import pytest

from assets import AssetManagement
from bus import InProcessEventBus
from engine import AgentEngine, CrewAIFramework, ExecutionContext
from engine._crewai_adapter import make_build_llm_factory
from files import PathPolicy
from llm import ProviderConfig, registry
from llm.providers.opencode import OpenCodeConnector
from providers import (
    NativeSandboxProvider,
    RecentBufferMemoryProvider,
    RuleBasedAuthorizationProvider,
)
from providers.secrets import DotenvSecretsProvider
from schemas import (
    AgentId,
    EventType,
    IOSpec,
    ModelSpec,
    ResolvedAgentConfig,
    RuntimeSpec,
)
from skills import SkillsSubsystem
from store import SessionStore, SqliteBackend
from tests.fixtures.assertions import assert_event_content, assert_event_sequence
from tests.fixtures.engine_builder import write_platform_yaml
from tools import ToolsSubsystem

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import crewai

    from llm import LLMCallContext


_TYPE = "opencode_wire_bug145"
_MISSING_SESSION_MESSAGE = "Request is missing x-opencode-session header"
_COMPLETION = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 0,
    "model": "m",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Fallback answered."},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}

Handler = Callable[[httpx.Request], httpx.Response]


def _status(status: int, body: dict[str, Any] | None = None) -> Handler:
    payload = body if body is not None else {"error": {"message": f"HTTP {status}"}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_COMPLETION)


class _WireOpenCodeConnector(OpenCodeConnector):
    """The real OpenCode connector, with a canned wire per provider entry.

    `build_llm` and `classify_call_error` are inherited unchanged; the
    only addition is the test transport + `max_retries = 0` on the
    client `build_llm` returned.
    """

    type_name: ClassVar[str] = _TYPE
    handlers: ClassVar[dict[str, Handler]] = {}
    calls: ClassVar[dict[str, int]] = {}

    async def build_llm(
        self,
        model_id: str,
        provider_config: ProviderConfig,
        api_key: str | None,
        parameters: Any = None,
        *,
        context: LLMCallContext | None = None,
    ) -> crewai.LLM:
        llm = await super().build_llm(
            model_id, provider_config, api_key, parameters, context=context
        )
        handler = self.handlers[provider_config.name]
        name = provider_config.name

        def counting(request: httpx.Request) -> httpx.Response:
            self.calls[name] = self.calls.get(name, 0) + 1
            return handler(request)

        llm.max_retries = 0
        llm.client_params = {
            "http_client": httpx.Client(transport=httpx.MockTransport(counting))
        }
        llm._client = None  # rebuilt from _get_client_params()
        return llm


@pytest.fixture(autouse=True)
def _wire_connector() -> Iterator[None]:
    _WireOpenCodeConnector.handlers.clear()
    _WireOpenCodeConnector.calls.clear()
    registry.register(_TYPE, _WireOpenCodeConnector)
    try:
        yield
    finally:
        registry._REGISTRY.pop(_TYPE, None)
        _WireOpenCodeConnector.handlers.clear()
        _WireOpenCodeConnector.calls.clear()


def _provider(name: str) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        type=_TYPE,
        api_key_secret="WIRE_KEY",
        base_url="https://gateway.invalid/v1",
        timeout_seconds=5,
    )


async def _run(
    tmp_path: Path,
    *,
    primary: Handler,
    fallback: Handler | None = None,
    max_retries: int = 0,
) -> list[dict[str, Any]]:
    """Drive one exchange; return the session's events as dicts."""
    _WireOpenCodeConnector.handlers["primary"] = primary
    providers = {"primary": _provider("primary")}
    fallback_models: list[ModelSpec] = []
    if fallback is not None:
        _WireOpenCodeConnector.handlers["backup"] = fallback
        providers["backup"] = _provider("backup")
        fallback_models = [ModelSpec(provider="backup", model="backup-model")]

    write_platform_yaml(tmp_path)
    (tmp_path / ".env").write_text("WIRE_KEY=sk-test\n")
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
    engine = AgentEngine(
        store=store,
        bus=InProcessEventBus(),
        asset_management=am,
        tools_subsystem=ToolsSubsystem(
            secrets=secrets,
            sandbox=sandbox,
            authz=authz,
            path_policy=PathPolicy(allowed_paths=["data/"], denied_paths=[".env"]),
            root=tmp_path,
        ),
        skills_subsystem=SkillsSubsystem(asset_management=am),
        memory_provider=RecentBufferMemoryProvider(store=store),
        secrets=secrets,
        sandbox=sandbox,
        authz=authz,
        framework=CrewAIFramework(),
        llm_providers_config=providers,
        llm_for_model=make_build_llm_factory(
            llm_providers_config=providers, secrets=secrets
        ),
    )
    config = ResolvedAgentConfig(
        schema_version="0.1",
        assembly_version="1",
        agent_id=AgentId("ag_test"),
        name="test-agent",
        description="A test agent.",
        model=ModelSpec(provider="primary", model="primary-model"),
        fallback_models=fallback_models,
        prompt="You are a helpful test agent.",
        io=IOSpec(mode="conversational"),
        runtime=RuntimeSpec(max_retries=max_retries),
    )
    session = await store.create_session("ag_test", name="wire")
    threads = await store.list_threads(session.session_id)
    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": "Hello, agent."},
    )
    return [
        event.model_dump(mode="json")
        async for event in engine.execute_stream(config, ctx)
    ]


_MODEL_AND_TERMINAL = {
    EventType.MODEL_REQUEST.value,
    EventType.MODEL_RESPONSE.value,
    EventType.MODEL_FALLBACK.value,
    EventType.THREAD_EXCHANGE_FAILED.value,
    EventType.THREAD_EXCHANGE_COMPLETED.value,
}


def _model_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e["type"] in _MODEL_AND_TERMINAL]


def _of_type(events: list[dict[str, Any]], kind: EventType) -> list[dict[str, Any]]:
    return [e for e in events if e["type"] == kind.value]


# ===========================================================================
# AC 7 — the ses_0cd8df9d incident
# ===========================================================================


async def test_missing_session_400_is_typed_bad_request_and_never_falls_back(
    tmp_path: Path,
) -> None:
    body = {
        "type": "error",
        "error": {"type": "MissingSessionID", "message": _MISSING_SESSION_MESSAGE},
    }
    events = await _run(tmp_path, primary=_status(400, body), fallback=_ok)

    model_events = _model_events(events)
    assert_event_sequence(
        model_events,
        ["model.request", "model.response", "thread.exchange_failed"],
    )
    response = model_events[1]
    assert_event_content(response, stop_reason="error")
    error = response["data"]["error"]
    assert error["type"] == "model.bad_request"
    assert _MISSING_SESSION_MESSAGE in error["detail"]["reason"]
    assert error["detail"]["provider"] == "primary"

    failed = model_events[2]["data"]["error"]
    assert failed["type"] == "model.bad_request"
    assert failed["where"] != "engine.execute.boundary"
    assert failed["where"] == "llm.connector.call"
    assert _WireOpenCodeConnector.calls == {"primary": 1}


# ===========================================================================
# AC 8 — Recoverable statuses advance the chain; Fatal ones don't
# ===========================================================================


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (429, "model.rate_limited"),
        (503, "model.server_error"),
        (404, "model.not_found"),
    ],
)
async def test_recoverable_status_falls_back_and_completes(
    tmp_path: Path, status: int, code: str
) -> None:
    events = await _run(tmp_path, primary=_status(status), fallback=_ok)

    model_events = _model_events(events)
    assert_event_sequence(
        model_events,
        [
            "model.request",
            "model.response",
            "model.fallback",
            "model.request",
            "model.response",
            "thread.exchange_completed",
        ],
    )
    assert model_events[1]["data"]["error"]["type"] == code
    assert_event_content(
        model_events[2],
        reason=code,
        attempt=1,
        **{
            "from": {"provider": "primary", "model": "primary-model"},
            "to": {"provider": "backup", "model": "backup-model"},
        },
    )
    assert model_events[4]["data"]["error"] is None
    assert _of_type(events, EventType.AGENT_MESSAGE)
    assert _WireOpenCodeConnector.calls == {"primary": 1, "backup": 1}


@pytest.mark.parametrize(
    ("status", "code"),
    [(400, "model.bad_request"), (401, "model.authentication_failed")],
)
async def test_fatal_status_does_not_fall_back(
    tmp_path: Path, status: int, code: str
) -> None:
    events = await _run(tmp_path, primary=_status(status), fallback=_ok)

    assert not _of_type(events, EventType.MODEL_FALLBACK)
    failed = _of_type(events, EventType.THREAD_EXCHANGE_FAILED)
    assert len(failed) == 1
    assert failed[0]["data"]["error"]["type"] == code
    assert "backup" not in _WireOpenCodeConnector.calls


async def test_404_without_chain_is_retried_by_the_engine_loop(
    tmp_path: Path,
) -> None:
    """QA NB-3: `model.not_found` is Recoverable, so with no chain the
    engine's retry loop re-sends it `max_retries + 1` times before
    `model.retry_exhausted`. No inference is spent (the 404 comes back
    before any), but the behaviour is now explicit."""
    events = await _run(tmp_path, primary=_status(404), max_retries=2)

    responses = _of_type(events, EventType.MODEL_RESPONSE)
    assert [r["data"]["error"]["type"] for r in responses] == ["model.not_found"] * 3
    failed = _of_type(events, EventType.THREAD_EXCHANGE_FAILED)
    assert len(failed) == 1
    assert failed[0]["data"]["error"]["type"] == "model.retry_exhausted"
    assert _WireOpenCodeConnector.calls == {"primary": 3}
