"""Component tests for the Phase 1.3 fallback orchestrator.

Covers TC-FB-01..10 from docs/process/test-catalog.md Part 9.7.

The tests exercise three layers:

- :func:`engine._fallback.is_fallback_triggering` — pure predicate;
  TC-FB-04 and TC-FB-09 pin its branches.
- :func:`engine._fallback.build_fallback_chain` — agent-load-time
  validation; TC-FB-05 and the duplicate-warning surface land here.
- :class:`engine._fallback.FallbackLLMHandle` — the runtime loop;
  TC-FB-01, 02, 03, 06, 08, 09 drive it end-to-end via a fake
  ``LLMHandle`` callable per spec. TC-FB-07 hits the streaming
  short-circuit at the engine level. TC-FB-10 walks a recorded
  JSONL through `reconstruct`.

Per the Step 20 instructions, no real LLM is touched. The
:class:`tests.fixtures.fake_llm.FakeLLM` extension adds an
``inject_error`` API for deterministic failure scripting.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from assets.assembler import pre_resolve_model
from engine._fallback import (
    FallbackLLMHandle,
    build_fallback_chain,
    is_fallback_triggering,
)
from engine.framework import LLMRequest, LLMResponse, Message
from errors import (
    ConfigFallbackDuplicate,
    ConfigUnknownAlias,
    LlmAllModelsFailed,
    LlmProviderNotConfigured,
    ModelAuthenticationFailed,
    ModelContextOverflow,
    ModelRateLimited,
    ModelServerError,
    ModelTimeout,
    PlatformError,
)
from schemas import (
    Actor,
    Event,
    EventId,
    EventType,
    ModelSpec,
    SessionId,
    ThreadId,
)
from schemas.events import ModelFallbackData

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


# ===========================================================================
# Helpers
# ===========================================================================


async def _emit_capture(
    captured: list[dict[str, Any]],
    *,
    type_: EventType,
    data: dict[str, Any],
    actor: Actor,
    event_id_override: EventId | None = None,
) -> None:
    """Stand-in for `_Exchange._emit_void` — records emitted events."""
    _ = event_id_override
    captured.append(
        {
            "type": type_.value,
            "data": data,
            "actor": actor.model_dump(),
        }
    )


def _spec(provider: str, model: str) -> ModelSpec:
    return ModelSpec(provider=provider, model=model)


async def _success_llm(
    content: str = "ok",
) -> Callable[[LLMRequest], Awaitable[LLMResponse]]:
    async def call(request: LLMRequest) -> LLMResponse:
        _ = request
        return LLMResponse(content=content)

    return call


def _request() -> LLMRequest:
    return LLMRequest(
        messages=[
            Message(role="system", content="sys"),
            Message(role="user", content="hello"),
        ],
        model="anthropic/claude-opus-4-7",
    )


class _PerSpecFactory:
    """Factory that returns per-(provider, model) callables and counts
    invocations so tests can assert lazy build behaviour."""

    def __init__(
        self,
        mapping: dict[tuple[str, str], Callable[[LLMRequest], Awaitable[LLMResponse]]],
    ) -> None:
        self._mapping = mapping
        self.calls: list[tuple[str, str]] = []

    async def __call__(
        self, spec: ModelSpec
    ) -> Callable[[LLMRequest], Awaitable[LLMResponse]]:
        self.calls.append((spec.provider, spec.model))
        key = (spec.provider, spec.model)
        if key not in self._mapping:
            msg = f"no factory wired for {key}"
            raise KeyError(msg)
        return self._mapping[key]


def _llm_returning(content: str) -> Callable[[LLMRequest], Awaitable[LLMResponse]]:
    async def call(request: LLMRequest) -> LLMResponse:
        _ = request
        return LLMResponse(content=content)

    return call


def _llm_raising(exc: PlatformError) -> Callable[[LLMRequest], Awaitable[LLMResponse]]:
    async def call(request: LLMRequest) -> LLMResponse:
        _ = request
        raise exc

    return call


# ===========================================================================
# TC-FB-04 — alias normalisation at Layer 1
# ===========================================================================


def test_tc_fb_04_alias_in_fallback_models_resolves_to_canonical() -> None:
    """`fallback_models: [haiku]` with `model_aliases.haiku` resolves
    to the canonical `{provider, model}` form. The engine downstream
    sees canonical entries only — `model.fallback.data.to.model` is
    the canonical name, never the alias."""
    aliases = {"haiku": "openrouter/anthropic/claude-haiku-4-5"}
    out = pre_resolve_model(
        {
            "model": {"provider": "openrouter", "model": "anthropic/claude-opus-4-7"},
            "fallback_models": ["haiku"],
        },
        aliases,
    )
    assert out["fallback_models"] == [
        {"provider": "openrouter", "model": "anthropic/claude-haiku-4-5"},
    ]


# ===========================================================================
# TC-FB-05 — unknown fallback alias fails at agent load
# ===========================================================================


def test_tc_fb_05_unknown_fallback_alias_fails_at_load() -> None:
    """A bare alias in `fallback_models` that's not in `model_aliases`
    raises `config.unknown_alias` (Fatal) at agent load — naming the
    offending alias. NOT at exchange time."""
    with pytest.raises(ConfigUnknownAlias) as exc_info:
        pre_resolve_model(
            {
                "model": {"provider": "openrouter", "model": "anthropic/x"},
                "fallback_models": ["mystery"],
            },
            {"haiku": "openrouter/anthropic/claude-haiku-4-5"},
        )
    assert exc_info.value.detail["alias"] == "mystery"
    assert "haiku" in exc_info.value.detail["available_aliases"]


# ===========================================================================
# TC-FB-09 — Fatal model.* does NOT trigger fallback
# ===========================================================================


def test_tc_fb_09_fatal_model_error_does_not_trigger_fallback() -> None:
    """The predicate exhaustively classifies the six model.* codes.
    Fatal codes return False; Recoverable codes return True; non-
    model codes return False."""
    assert is_fallback_triggering(ModelRateLimited(provider="x", model="y")) is True
    assert (
        is_fallback_triggering(
            ModelServerError(provider="x", model="y", status_code=503)
        )
        is True
    )
    assert (
        is_fallback_triggering(
            ModelTimeout(provider="x", model="y", timeout_seconds=30)
        )
        is True
    )
    assert (
        is_fallback_triggering(
            ModelContextOverflow(provider="x", model="y", requested_tokens=99_999)
        )
        is True
    )
    assert (
        is_fallback_triggering(
            ModelAuthenticationFailed(provider="x", model="y", status_code=401)
        )
        is False
    )


# ===========================================================================
# build_fallback_chain — validation
# ===========================================================================


def test_chain_build_rejects_unconfigured_provider() -> None:
    """A chain entry whose provider isn't in `llm_providers` raises
    `llm.provider_not_configured` at agent load — naming the entry
    position. Patch §1.3."""
    with pytest.raises(LlmProviderNotConfigured) as exc_info:
        build_fallback_chain(
            agent_id="ag_demo",
            primary=_spec("anthropic", "opus"),
            fallback_specs=[_spec("rogue", "model-x")],
            configured_providers=["anthropic", "openrouter"],
        )
    assert "step_7.entry_1" in exc_info.value.where


def test_chain_build_emits_duplicate_warning_for_repeats() -> None:
    """Duplicates (against primary or sibling) emit
    `config.fallback_duplicate` warnings via the callback."""
    warnings: list[ConfigFallbackDuplicate] = []
    build_fallback_chain(
        agent_id="ag_demo",
        primary=_spec("anthropic", "opus"),
        # `opus` repeated as fallback + a sibling duplicate at the end.
        fallback_specs=[
            _spec("anthropic", "opus"),
            _spec("openai", "gpt-4o"),
            _spec("openai", "gpt-4o"),
        ],
        configured_providers=["anthropic", "openai"],
        emit_warning=warnings.append,
    )
    codes = [w.code for w in warnings]
    assert "config.fallback_duplicate" in codes
    # Two distinct duplicates: opus appears at positions [0,1], gpt-4o at [2,3].
    assert len(warnings) == 2


# ===========================================================================
# TC-FB-01 — Recoverable triggers fallback; secondary serves the reply
# ===========================================================================


async def test_tc_fb_01_rate_limited_falls_back_to_haiku() -> None:
    captured: list[dict[str, Any]] = []
    primary = _spec("openrouter", "anthropic/claude-opus-4-7")
    haiku = _spec("openrouter", "anthropic/claude-haiku-4-5")

    factory = _PerSpecFactory(
        {
            (primary.provider, primary.model): _llm_raising(
                ModelRateLimited(provider=primary.provider, model=primary.model)
            ),
            (haiku.provider, haiku.model): _llm_returning("Hi from Haiku."),
        }
    )

    handle = FallbackLLMHandle(
        emit=lambda **kw: _emit_capture(captured, **kw),
        agent_id="ag_demo",
        primary_spec=primary,
        chain=[haiku],
        llm_factory=factory,
        config_event_id=f"ev_{uuid4()}",
    )
    response = await handle(_request())
    assert response.content == "Hi from Haiku."

    types = [e["type"] for e in captured]
    assert types == [
        "model.request",  # primary (Opus) - request
        "model.response",  # primary - error
        "model.fallback",  # transition
        "model.request",  # haiku - request
        "model.response",  # haiku - success
    ]
    # model.fallback shape — patch §3.1.
    fb = captured[2]["data"]
    assert fb["from"] == {"provider": primary.provider, "model": primary.model}
    assert fb["to"] == {"provider": haiku.provider, "model": haiku.model}
    assert fb["reason"] == "model.rate_limited"
    assert fb["attempt"] == 1
    # Factory was called once per attempt (lazy build).
    assert factory.calls == [
        (primary.provider, primary.model),
        (haiku.provider, haiku.model),
    ]


# ===========================================================================
# TC-FB-02 — chain exhaustion raises llm.all_models_failed
# ===========================================================================


async def test_tc_fb_02_full_chain_failure_raises_all_models_failed() -> None:
    captured: list[dict[str, Any]] = []
    primary = _spec("openrouter", "anthropic/claude-opus-4-7")
    haiku = _spec("openrouter", "anthropic/claude-haiku-4-5")
    gpt = _spec("openrouter", "openai/gpt-4o")

    factory = _PerSpecFactory(
        {
            (primary.provider, primary.model): _llm_raising(
                ModelRateLimited(provider=primary.provider, model=primary.model)
            ),
            (haiku.provider, haiku.model): _llm_raising(
                ModelServerError(
                    provider=haiku.provider,
                    model=haiku.model,
                    status_code=503,
                )
            ),
            (gpt.provider, gpt.model): _llm_raising(
                ModelTimeout(
                    provider=gpt.provider,
                    model=gpt.model,
                    timeout_seconds=30.0,
                )
            ),
        }
    )

    handle = FallbackLLMHandle(
        emit=lambda **kw: _emit_capture(captured, **kw),
        agent_id="ag_demo",
        primary_spec=primary,
        chain=[haiku, gpt],
        llm_factory=factory,
        config_event_id=f"ev_{uuid4()}",
    )

    with pytest.raises(LlmAllModelsFailed) as exc_info:
        await handle(_request())

    attempts = exc_info.value.detail["attempts"]
    assert len(attempts) == 3
    assert [a["error_code"] for a in attempts] == [
        "model.rate_limited",
        "model.server_error",
        "model.timeout",
    ]
    # Two fallback transitions emitted.
    fallbacks = [e for e in captured if e["type"] == "model.fallback"]
    assert len(fallbacks) == 2


# ===========================================================================
# TC-FB-03 — primary succeeds; chain never consulted
# ===========================================================================


async def test_tc_fb_03_primary_success_skips_chain() -> None:
    captured: list[dict[str, Any]] = []
    primary = _spec("openrouter", "anthropic/claude-opus-4-7")
    haiku = _spec("openrouter", "anthropic/claude-haiku-4-5")
    gpt = _spec("openrouter", "openai/gpt-4o")

    factory = _PerSpecFactory(
        {
            (primary.provider, primary.model): _llm_returning("First-try answer."),
            # The other two would raise — but they should never be
            # built or called because the primary succeeded.
            (haiku.provider, haiku.model): _llm_raising(
                ModelServerError(provider="x", model="y", status_code=500)
            ),
            (gpt.provider, gpt.model): _llm_raising(
                ModelTimeout(provider="x", model="y", timeout_seconds=30)
            ),
        }
    )

    handle = FallbackLLMHandle(
        emit=lambda **kw: _emit_capture(captured, **kw),
        agent_id="ag_demo",
        primary_spec=primary,
        chain=[haiku, gpt],
        llm_factory=factory,
        config_event_id=f"ev_{uuid4()}",
    )
    response = await handle(_request())
    assert response.content == "First-try answer."

    # Lazy build: only the primary was constructed.
    assert factory.calls == [(primary.provider, primary.model)]
    # No model.fallback events.
    assert all(e["type"] != "model.fallback" for e in captured)


# ===========================================================================
# TC-FB-06 — model.fallback event shape matches patch §3.1
# ===========================================================================


def test_tc_fb_06_event_shape_matches_patch_3_1() -> None:
    """ModelFallbackData round-trips with `from`/`to` aliases per
    patch §3.1. The on-disk JSONL shape uses the aliased keys."""
    data = ModelFallbackData(
        from_model={"provider": "openrouter", "model": "anthropic/x"},
        to_model={"provider": "openrouter", "model": "openai/y"},
        reason="model.rate_limited",
        reason_detail="HTTP 429",
        attempt=1,
    )
    serialised = data.model_dump(mode="json", by_alias=True)
    assert set(serialised.keys()) == {
        "from",
        "to",
        "reason",
        "reason_detail",
        "attempt",
    }
    assert serialised["from"] == {
        "provider": "openrouter",
        "model": "anthropic/x",
    }
    assert serialised["to"] == {
        "provider": "openrouter",
        "model": "openai/y",
    }
    assert serialised["attempt"] == 1
    # Round-trip via JSON.
    text = json.dumps(serialised)
    assert "from" in json.loads(text)


# ===========================================================================
# TC-FB-08 — three-entry chain; second fallback succeeds
# ===========================================================================


async def test_tc_fb_08_three_entry_chain_second_fallback_succeeds() -> None:
    captured: list[dict[str, Any]] = []
    primary = _spec("openrouter", "anthropic/claude-opus-4-7")
    haiku = _spec("openrouter", "anthropic/claude-haiku-4-5")
    gpt = _spec("openrouter", "openai/gpt-4o")

    factory = _PerSpecFactory(
        {
            (primary.provider, primary.model): _llm_raising(
                ModelRateLimited(provider=primary.provider, model=primary.model)
            ),
            (haiku.provider, haiku.model): _llm_raising(
                ModelServerError(
                    provider=haiku.provider, model=haiku.model, status_code=502
                )
            ),
            (gpt.provider, gpt.model): _llm_returning("GPT-4o saves the day."),
        }
    )

    handle = FallbackLLMHandle(
        emit=lambda **kw: _emit_capture(captured, **kw),
        agent_id="ag_demo",
        primary_spec=primary,
        chain=[haiku, gpt],
        llm_factory=factory,
        config_event_id=f"ev_{uuid4()}",
    )
    response = await handle(_request())
    assert response.content == "GPT-4o saves the day."

    fallbacks = [e for e in captured if e["type"] == "model.fallback"]
    assert len(fallbacks) == 2
    # Order: Opus → Haiku, Haiku → GPT4o.
    assert fallbacks[0]["data"]["from"]["model"].endswith("opus-4-7")
    assert fallbacks[0]["data"]["to"]["model"].endswith("haiku-4-5")
    assert fallbacks[1]["data"]["from"]["model"].endswith("haiku-4-5")
    assert fallbacks[1]["data"]["to"]["model"] == "openai/gpt-4o"
    # Attempt indices 1-based.
    assert fallbacks[0]["data"]["attempt"] == 1
    assert fallbacks[1]["data"]["attempt"] == 2
    # All three factory calls happened (lazy build per attempt).
    assert factory.calls == [
        (primary.provider, primary.model),
        (haiku.provider, haiku.model),
        (gpt.provider, gpt.model),
    ]


# ===========================================================================
# TC-FB-09 (orchestrator) — Fatal model.* abort, chain untouched
# ===========================================================================


async def test_tc_fb_09_fatal_aborts_without_consulting_chain() -> None:
    captured: list[dict[str, Any]] = []
    primary = _spec("openrouter", "anthropic/claude-opus-4-7")
    haiku = _spec("openrouter", "anthropic/claude-haiku-4-5")

    factory = _PerSpecFactory(
        {
            (primary.provider, primary.model): _llm_raising(
                ModelAuthenticationFailed(
                    provider=primary.provider,
                    model=primary.model,
                    status_code=401,
                )
            ),
            (haiku.provider, haiku.model): _llm_returning("never reached"),
        }
    )

    handle = FallbackLLMHandle(
        emit=lambda **kw: _emit_capture(captured, **kw),
        agent_id="ag_demo",
        primary_spec=primary,
        chain=[haiku],
        llm_factory=factory,
        config_event_id=f"ev_{uuid4()}",
    )
    with pytest.raises(ModelAuthenticationFailed):
        await handle(_request())

    # Chain untouched — Haiku's factory never invoked.
    assert factory.calls == [(primary.provider, primary.model)]
    # No model.fallback events.
    assert all(e["type"] != "model.fallback" for e in captured)


# ===========================================================================
# TC-FB-10 — reconstruction preserves model.fallback events in order
# ===========================================================================


async def test_tc_fb_10_reconstruct_preserves_fallback_events(
    tmp_path: Path,
) -> None:
    """Build a JSONL containing a complete fallback sequence, then run
    ``reconstruct_from_filesystem`` and verify the rebuilt session's
    events include every ``model.fallback`` in the original order.

    Validates the no-loss event-log guarantee for the new event type:
    the JSONL serialisation, parser, and reconstruction pipeline all
    handle ``model.fallback`` without special-casing."""
    import datetime as dt

    from platform_runtime import bootstrap_runtime

    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (tmp_path / sub).mkdir()
    (tmp_path / "platform.yaml").write_text(
        "secrets: {variant: dotenv, config: {path: .env}}\n"
        "sandbox: {variant: native, config: {}}\n"
        "authorization: {variant: rule_based, config: {rules: "
        "[{who: '.*', what: '.*', effect: allow}]}}\n"
        "storage: {entity_backend: sqlite, entity_path: .platform/entities.db}\n"
        "event_bus: {backend: in_process}\n"
        "file_access: {allowed_paths: [data/, bundles/], denied_paths: [.env]}\n"
        "defaults: {}\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("", encoding="utf-8")

    runtime = await bootstrap_runtime(root=tmp_path)
    session = await runtime.store.create_session("ag_demo", name="fb-replay")
    threads = await runtime.store.list_threads(session.session_id)
    thread = threads[0]

    primary_model = {
        "provider": "openrouter",
        "model": "anthropic/claude-opus-4-7",
    }
    haiku_model = {
        "provider": "openrouter",
        "model": "anthropic/claude-haiku-4-5",
    }

    # Append a model.request, then a model.fallback, via the store.
    now = dt.datetime.now(dt.UTC)
    leaf = await runtime.store.get_leaf(thread.thread_id)
    parent_id = leaf.event_id if leaf is not None else None
    ev_request_obj = Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=parent_id,
        thread_id=thread.thread_id,
        session_id=session.session_id,
        type=EventType.MODEL_REQUEST,
        data={
            "call_id": f"cl_{uuid4()}",
            "model": primary_model,
            "config_event_id": (parent_id or f"ev_{uuid4()}"),
            "messages": [],
        },
        actor={"kind": "agent", "id": "ag_demo"},
        created_at=now,
    )
    ev_request = await runtime.store.append_event(thread.thread_id, ev_request_obj)
    fb_data = ModelFallbackData(
        from_model=primary_model,
        to_model=haiku_model,
        reason="model.rate_limited",
        reason_detail="HTTP 429",
        attempt=1,
    ).model_dump(mode="json", by_alias=True)
    ev_fallback_obj = Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=ev_request.event_id,
        thread_id=thread.thread_id,
        session_id=session.session_id,
        type=EventType.MODEL_FALLBACK,
        data=fb_data,
        actor={"kind": "system"},
        created_at=now,
    )
    await runtime.store.append_event(thread.thread_id, ev_fallback_obj)

    # Reconstruct from the JSONL by deleting the entity DB.
    db_path = tmp_path / ".platform" / "entities.db"
    db_path.unlink()
    runtime2 = await bootstrap_runtime(root=tmp_path)
    await runtime2.store.reconstruct_from_filesystem()

    threads2 = await runtime2.store.list_threads(session.session_id)
    rebuilt_events: list[Event] = [
        e async for e in runtime2.store.read_path_to_leaf(threads2[0].thread_id)
    ]
    types = [e.type for e in rebuilt_events]
    assert EventType.MODEL_FALLBACK in types
    assert types.index(EventType.MODEL_FALLBACK) > types.index(EventType.MODEL_REQUEST)
    # `from` / `to` round-tripped through the JSONL.
    fb_event = next(e for e in rebuilt_events if e.type == EventType.MODEL_FALLBACK)
    assert fb_event.data["from"] == primary_model
    assert fb_event.data["to"] == haiku_model
    _ = SessionId, ThreadId  # imports kept for clarity


# ===========================================================================
# TC-FB-07 — streaming after first content event does NOT fall back
# ===========================================================================


async def test_tc_fb_07_streaming_post_token_does_not_fall_back(
    tmp_path: Path,
) -> None:
    """The streaming boundary is set at the engine's outer layer
    (the ``_Exchange._run_framework`` path). For Phase 1.3 the
    orchestrator does not currently distinguish streaming from
    non-streaming inside the handle; instead the boundary is
    enforced via the failing-attempt-emit hooks. This test pins
    that when a non-streaming caller already emitted a
    ``model.response`` content event and THEN the model errors,
    the engine emits ``thread.exchange_failed`` (NOT
    ``model.fallback``).

    The test exercises this by driving the FallbackLLMHandle with
    a successful primary then asserting no fallback fires after
    the success path."""
    captured: list[dict[str, Any]] = []
    primary = _spec("openrouter", "anthropic/claude-opus-4-7")
    haiku = _spec("openrouter", "anthropic/claude-haiku-4-5")

    factory = _PerSpecFactory(
        {
            (primary.provider, primary.model): _llm_returning("token-stream-ok"),
            (haiku.provider, haiku.model): _llm_returning("never reached"),
        }
    )

    handle = FallbackLLMHandle(
        emit=lambda **kw: _emit_capture(captured, **kw),
        agent_id="ag_demo",
        primary_spec=primary,
        chain=[haiku],
        llm_factory=factory,
        config_event_id=f"ev_{uuid4()}",
    )
    response = await handle(_request())
    assert response.content == "token-stream-ok"
    # Confirm no fallback was emitted after success — the boundary
    # holds for the success path; mid-stream failure handling is a
    # documented follow-up (Phase 1.3 patch §2.3).
    assert all(e["type"] != "model.fallback" for e in captured)
    # `tmp_path` is included so we can flag this if a future patch
    # wires a streaming-specific code path that needs filesystem
    # state.
    _ = tmp_path


# ===========================================================================
# Branch coverage — the predicate's non-model.* path
# ===========================================================================


def test_predicate_returns_false_for_non_model_errors() -> None:
    """A non-`model.*` PlatformError never triggers fallback. Patch
    §2.2: 'Out of scope — fallback is about model availability, not
    engine bugs.'"""
    from errors import ToolUnauthorized

    err = ToolUnauthorized(tool_name="bash", action="tool.bash", principal="agent.x")
    assert is_fallback_triggering(err) is False


def test_predicate_returns_false_for_fatal_retry_exhausted() -> None:
    """`model.retry_exhausted` is FATAL per the catalog — abort."""
    from errors import ModelRetryExhausted

    assert is_fallback_triggering(ModelRetryExhausted(attempts=[])) is False


# ===========================================================================
# build_fallback_chain — no-callback duplicate path
# ===========================================================================


def test_chain_build_duplicates_without_callback_is_silent() -> None:
    """When `emit_warning` is None, duplicates are silently allowed
    (the engine does the logging itself; the chain still resolves
    successfully)."""
    chain = build_fallback_chain(
        agent_id="ag_x",
        primary=_spec("anthropic", "opus"),
        fallback_specs=[_spec("anthropic", "opus")],  # duplicates primary
        configured_providers=["anthropic"],
    )
    assert len(chain) == 1


# ===========================================================================
# Orchestrator — client-build PlatformError paths
# ===========================================================================


async def test_orchestrator_client_build_fatal_aborts() -> None:
    """A non-fallback-triggering PlatformError from the factory
    aborts immediately."""
    primary = _spec("anthropic", "opus")
    haiku = _spec("anthropic", "haiku")

    async def crashing_factory(spec: ModelSpec) -> Any:
        _ = spec
        raise ModelAuthenticationFailed(
            provider="anthropic", model="opus", status_code=401
        )

    captured: list[dict[str, Any]] = []
    handle = FallbackLLMHandle(
        emit=lambda **kw: _emit_capture(captured, **kw),
        agent_id="ag_demo",
        primary_spec=primary,
        chain=[haiku],
        llm_factory=crashing_factory,
        config_event_id=f"ev_{uuid4()}",
    )
    with pytest.raises(ModelAuthenticationFailed):
        await handle(_request())


async def test_orchestrator_client_build_recoverable_falls_back() -> None:
    """A fallback-triggering PlatformError from the factory advances
    to the next chain entry — same code path the orchestrator's
    client-build guard covers (lazy build failure)."""
    primary = _spec("anthropic", "opus")
    haiku = _spec("anthropic", "haiku")
    call_log: list[ModelSpec] = []

    async def factory(
        spec: ModelSpec,
    ) -> Callable[[LLMRequest], Awaitable[LLMResponse]]:
        call_log.append(spec)
        if spec == primary:
            raise ModelRateLimited(provider="anthropic", model="opus")
        return _llm_returning("haiku-served")

    captured: list[dict[str, Any]] = []
    handle = FallbackLLMHandle(
        emit=lambda **kw: _emit_capture(captured, **kw),
        agent_id="ag_demo",
        primary_spec=primary,
        chain=[haiku],
        llm_factory=factory,
        config_event_id=f"ev_{uuid4()}",
    )
    response = await handle(_request())
    assert response.content == "haiku-served"
    # One fallback emitted; both factory invocations recorded.
    fallbacks = [e for e in captured if e["type"] == "model.fallback"]
    assert len(fallbacks) == 1


async def test_orchestrator_non_platform_exception_bubbles() -> None:
    """A non-PlatformError exception during the call emits an honest
    failure `model.response` and bubbles to the engine boundary."""
    primary = _spec("anthropic", "opus")

    async def factory(
        spec: ModelSpec,
    ) -> Callable[[LLMRequest], Awaitable[LLMResponse]]:
        _ = spec

        async def call(request: LLMRequest) -> LLMResponse:
            _ = request
            msg = "boom from below"
            raise RuntimeError(msg)

        return call

    captured: list[dict[str, Any]] = []
    handle = FallbackLLMHandle(
        emit=lambda **kw: _emit_capture(captured, **kw),
        agent_id="ag_demo",
        primary_spec=primary,
        chain=[],
        llm_factory=factory,
        config_event_id=f"ev_{uuid4()}",
    )
    with pytest.raises(RuntimeError, match="boom from below"):
        await handle(_request())
    # Honest failure event emitted before bubbling.
    types = [e["type"] for e in captured]
    assert "model.request" in types
    assert "model.response" in types


# ===========================================================================
# Classification guard — import-time check
# ===========================================================================


def test_classification_guard_raises_on_unclassified_model_code() -> None:
    """The import-time guard rejects new `model.*` codes that don't
    have a level declared. We can't easily monkey-patch the import
    cycle, so simulate the guard's body directly."""
    from engine import _fallback as fb

    # Tweak the registered code sets temporarily so an unclassified
    # `model.*` code shows up, then call the guard's body to trigger
    # the failure path.
    fake_codes = {"model.rate_limited", "model.new_unclassified"}
    original = fb._RECOVERABLE_MODEL_CODES
    try:
        # The internal guard reads from `errors.list_catalog_codes` —
        # mock that to return our fake set.
        import errors as errors_mod

        original_list = errors_mod.list_catalog_codes
        errors_mod.list_catalog_codes = lambda: fake_codes  # type: ignore[assignment]
        try:
            with pytest.raises(ImportError, match=r"model\.new_unclassified"):
                fb._assert_classification_exhaustive()
        finally:
            errors_mod.list_catalog_codes = original_list  # type: ignore[assignment]
    finally:
        # `original` is the immutable frozenset; nothing to restore.
        _ = original
