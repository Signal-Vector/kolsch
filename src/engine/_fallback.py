"""Engine fallback orchestrator — Phase 1.3 (Step 20).

Three things live here:

- :func:`is_fallback_triggering` — the predicate the engine consults
  to decide whether a Recoverable ``model.*`` error should walk to
  the next chain entry. Pure, exhaustive over the catalog's
  ``model.*`` codes; import-time validation raises ``ImportError``
  if a new ``model.*`` code lands without a classification (AGENTS.md
  hard rule, FB-R10).
- :func:`build_fallback_chain` — resolves and validates an agent's
  fallback chain at agent-load time. Each entry's provider must
  appear in ``llm_providers``; emits
  :class:`ConfigFallbackDuplicate` (Diagnostic) warnings for repeats
  (not errors — a user may have listed the same model twice
  intentionally per patch §2.4). Returns
  ``list[ModelSpec]``; clients are NOT built here (lazy
  construction is the engine loop's job — patch §2.1 step 7).
- :class:`FallbackLLMHandle` — the framework-facing LLMHandle. Wraps
  the per-attempt observation that ``_ObservedLLMHandle`` does
  today, plus the chain loop: try primary → on Recoverable, emit
  ``model.fallback`` and advance → on Fatal, abort → on
  exhaustion, raise :class:`LlmAllModelsFailed`.

The orchestrator does not "remember" the chain across exchanges —
each exchange starts from the primary. Sticky preferences are a
Phase 3 hosted concern.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from errors import (
    ConfigFallbackDuplicate,
    LlmAllModelsFailed,
    LlmProviderNotConfigured,
    ModelAuthenticationFailed,
    ModelBadRequest,
    ModelContextOverflow,
    ModelRateLimited,
    ModelServerError,
    ModelTimeout,
    PlatformError,
)
from schemas import (
    Actor,
    AgentCallId,
    Error,
    EventId,
    EventType,
    ModelFallbackData,
    ModelRequestData,
    ModelResponseData,
    ModelSpec,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from engine.framework import LLMRequest, LLMResponse


_log = logging.getLogger("engine.fallback")


# ---------------------------------------------------------------------------
# Classification predicate
# ---------------------------------------------------------------------------


# Exhaustive list of `model.*` codes that drive the fallback loop.
# Adding a new `model.*` code without listing it here or in
# :data:`_FATAL_MODEL_CODES` causes :func:`_assert_classification_exhaustive`
# to fail at import time — that's the AGENTS.md hard rule (FB-R10)
# enforced in code.
_RECOVERABLE_MODEL_CODES: frozenset[str] = frozenset(
    {
        "model.rate_limited",
        "model.server_error",
        "model.timeout",
        "model.context_overflow",
        # Phase 1.1 generic, kept Recoverable in the catalog. Treated
        # as fallback-triggering for back-compat.
        "model.upstream_error",
        # Phase 1.1 / engine-side parse failure. Recoverable per the
        # catalog; the chain may have a more reliable model.
        "model.malformed_response",
    }
)

_FATAL_MODEL_CODES: frozenset[str] = frozenset(
    {
        "model.authentication_failed",
        "model.bad_request",
        # Phase 1.1 — chain won't help (we already retried).
        "model.retry_exhausted",
    }
)


def is_fallback_triggering(err: PlatformError) -> bool:
    """Return True iff `err` is a Recoverable `model.*` error and the
    engine should advance to the next chain entry.

    Behaviour summary:

    - `err.code` in :data:`_RECOVERABLE_MODEL_CODES` → True.
    - `err.code` in :data:`_FATAL_MODEL_CODES` → False.
    - Anything not in `model.*` → False (out of scope per patch §2.2:
      fallback is about model availability, not engine bugs).
    - A `model.*` code not in either set → import-time `ImportError`
      ensures this never happens at runtime (see
      :func:`_assert_classification_exhaustive`).
    """
    code = err.code
    if code in _RECOVERABLE_MODEL_CODES:
        return True
    if code in _FATAL_MODEL_CODES:
        return False
    # Non-model.* — never triggers fallback.
    return False


def _assert_classification_exhaustive() -> None:
    """Import-time guard for AGENTS.md FB-R10.

    Walk the registered catalog; any `model.*` code that is neither
    in :data:`_RECOVERABLE_MODEL_CODES` nor :data:`_FATAL_MODEL_CODES`
    raises ``ImportError`` naming the unclassified code. The fix is
    to declare the code's level in
    :mod:`errors.fallback` (or its sibling module) AND add it to the
    classification set here in the SAME change.
    """
    from errors import list_catalog_codes  # noqa: PLC0415

    unclassified: list[str] = []
    for code in list_catalog_codes():
        if not code.startswith("model."):
            continue
        if code in _RECOVERABLE_MODEL_CODES:
            continue
        if code in _FATAL_MODEL_CODES:
            continue
        unclassified.append(code)
    if unclassified:
        msg = (
            f"AGENTS.md hard rule (FB-R10): the following `model.*` "
            f"codes are missing fallback classification — declare each "
            f"as Recoverable or Fatal in src/engine/_fallback.py and "
            f"add it to the trigger table in "
            f"docs/explanation/error-model.md §4.15: {sorted(unclassified)}"
        )
        raise ImportError(msg)


_assert_classification_exhaustive()


# ---------------------------------------------------------------------------
# Chain construction (agent-load time)
# ---------------------------------------------------------------------------


def build_fallback_chain(
    agent_id: str,
    fallback_specs: list[ModelSpec],
    primary: ModelSpec,
    configured_providers: list[str],
    *,
    emit_warning: Callable[[ConfigFallbackDuplicate], None] | None = None,
) -> list[ModelSpec]:
    """Validate the fallback chain at agent-load time.

    Spec §2.1 step 7 + patch §1.3:

    - Each entry's ``provider`` must be in ``configured_providers``;
      if not, raises :class:`LlmProviderNotConfigured` naming the
      offending entry position.
    - Duplicates (either against the primary or another entry) are
      allowed but emit :class:`ConfigFallbackDuplicate` (Diagnostic).
      ``emit_warning`` is the surface — production wires it to the
      CLI banner; tests collect to a list.
    - The list is returned as-is (post-Layer-1 normalisation; the
      assembler already canonicalised each entry).

    Does NOT build ``crewai.LLM`` clients — that's the engine loop's
    job at use time.
    """
    # Duplicate detection — keyed by canonical (provider, model).
    seen: dict[tuple[str, str], list[int]] = {}
    primary_key = (primary.provider, primary.model)
    seen[primary_key] = [0]  # position 0 = primary
    for idx, entry in enumerate(fallback_specs, start=1):
        if entry.provider not in configured_providers:
            raise LlmProviderNotConfigured(
                provider=entry.provider,
                configured_providers=list(configured_providers),
                where=f"engine.preflight.step_7.entry_{idx}",
            )
        key = (entry.provider, entry.model)
        seen.setdefault(key, []).append(idx)

    if emit_warning is not None:
        for (provider, model), positions in seen.items():
            if len(positions) <= 1:
                continue
            emit_warning(
                ConfigFallbackDuplicate(
                    agent=agent_id,
                    model={"provider": provider, "model": model},
                    positions=positions,
                )
            )
    return list(fallback_specs)


# ---------------------------------------------------------------------------
# Streaming boundary — patch §2.3
# ---------------------------------------------------------------------------


class StreamState:
    """Per-call streaming-boundary signal — Phase 1.3 patch §2.3.

    Carries one boolean: has the connector emitted any content
    token to the user yet? The :class:`FallbackLLMHandle`
    Recoverable branch consults this on every retry attempt. Once
    the flag is set, fallback is short-circuited — partial output
    has been committed to the log + the user's terminal; swapping
    models mid-stream would lie about which model produced the
    answer.

    Wiring:

    - The engine constructs one :class:`StreamState` per
      ``execute_stream`` call and passes it to the handle.
    - The connector's streaming layer calls
      :meth:`mark_content_emitted` on the first content chunk.
    - The handle's orchestrator polls
      :meth:`has_emitted_content` before advancing on a
      Recoverable error; if True, it re-raises the original error
      without emitting a ``model.fallback`` event.

    Non-streaming callers don't need this — they pass ``None`` and
    the orchestrator behaves exactly as before. The current Phase
    1.1 / 1.2 connectors do not stream through the handle (they
    return one ``LLMResponse``); when a streaming connector lands,
    it sets this flag on the first token. The contract is
    front-loaded so connector authors implement against a real
    seam, not a TODO.
    """

    __slots__ = ("_emitted",)

    def __init__(self) -> None:
        self._emitted = False

    def mark_content_emitted(self) -> None:
        """Set the flag. Idempotent."""
        self._emitted = True

    def has_emitted_content(self) -> bool:
        """Return whether any content token has been emitted to the
        user since this StreamState was constructed."""
        return self._emitted


# ---------------------------------------------------------------------------
# The orchestrator — framework-facing LLMHandle
# ---------------------------------------------------------------------------


# Type aliases for clarity. The factory is async because production
# build_llm is async (it calls Secrets.resolve); tests can wrap a
# sync callable in `_sync_factory` below.
if TYPE_CHECKING:
    LLMCallable = Callable[["LLMRequest"], Awaitable["LLMResponse"]]
    LLMFactory = Callable[[ModelSpec], Awaitable[LLMCallable]]


class FallbackLLMHandle:
    """Engine-controlled LLMHandle implementing the Phase 1.3
    fallback loop.

    Each ``__call__(request)`` walks the chain:

    1. Try primary → on success, emit ``model.request`` /
       ``model.response`` AND return.
    2. On Recoverable ``model.*``: emit a failure
       ``model.response`` (stop_reason=error), then a
       ``model.fallback`` event, then re-issue against the next
       chain entry's lazily-built LLMCallable. Repeat.
    3. On Fatal ``model.*``: emit the failure ``model.response``
       and re-raise the original PlatformError. Chain is NOT
       consulted.
    4. On chain exhaustion: raise :class:`LlmAllModelsFailed` with
       the full attempts list.

    Streaming is supported by the engine's outer layer; this handle
    does not currently distinguish stream-pre-token-emitted from
    stream-post-token-emitted (the engine sets
    :attr:`stream_pre_first_token` on the request context to gate
    fallback at the outer level — see :mod:`engine.engine`'s
    streaming path).
    """

    def __init__(
        self,
        *,
        emit: Callable[..., Awaitable[None]],
        agent_id: str,
        primary_spec: ModelSpec,
        chain: list[ModelSpec],
        llm_factory: LLMFactory,
        config_event_id: str | None,
        stream_state: StreamState | None = None,
    ) -> None:
        self._emit = emit
        self._agent_id = agent_id
        self._primary_spec = primary_spec
        self._chain = chain
        self._llm_factory = llm_factory
        self._config_event_id = config_event_id
        # Phase 1.3 patch §2.3 — populated by the engine's streaming
        # path; None for non-streaming exchanges. When set, the
        # orchestrator's Recoverable branch consults
        # `stream_state.has_emitted_content()` before advancing.
        self._stream_state = stream_state
        # Production callers can observe `factory_calls` to confirm
        # lazy-build behaviour (TC-FB-03 etc).
        self.factory_calls: list[ModelSpec] = []

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        all_specs = [self._primary_spec, *self._chain]
        attempts: list[dict[str, Any]] = []
        previous_spec: ModelSpec | None = None
        previous_error: PlatformError | None = None

        for attempt_idx, spec in enumerate(all_specs):
            # Emit `model.fallback` BEFORE the next attempt's
            # `model.request` (patch §3.2 ordering).
            if previous_spec is not None and previous_error is not None:
                await self._emit_fallback(
                    from_spec=previous_spec,
                    to_spec=spec,
                    previous_error=previous_error,
                    attempt=attempt_idx,
                )

            # Lazy LLM construction — only built when we actually
            # need this entry.
            try:
                llm_callable = await self._llm_factory(spec)
            except PlatformError as exc:
                # Treat client-build failures as fallback-triggering
                # for the chain entries (per patch §1.3: a fallback
                # to an unconfigured provider should have failed at
                # agent load — if it surfaces here, the chain is
                # mis-resolved).
                if not is_fallback_triggering(exc):
                    raise
                if self._stream_emitted_content():
                    # Phase 1.3 patch §2.3 streaming boundary —
                    # partial output already committed; abort with
                    # the original error rather than swapping models
                    # mid-stream.
                    raise
                attempts.append(_attempt_entry(spec, exc))
                previous_spec = spec
                previous_error = exc
                continue
            self.factory_calls.append(spec)

            call_id = AgentCallId(f"cl_{uuid4()}")
            await self._emit_request(call_id=call_id, spec=spec, request=request)

            try:
                response = await llm_callable(request)
            except PlatformError as exc:
                await self._emit_response_error(call_id=call_id, exc=exc)
                attempts.append(_attempt_entry(spec, exc))
                if not is_fallback_triggering(exc):
                    # Fatal `model.*` (or anything else typed) — abort
                    # without consulting the rest of the chain.
                    raise
                if self._stream_emitted_content():
                    # Phase 1.3 patch §2.3 streaming boundary —
                    # post-token-emitted Recoverable does NOT
                    # advance; re-raise the original Recoverable so
                    # the engine emits `thread.exchange_failed` with
                    # the truncation visible.
                    raise
                previous_spec = spec
                previous_error = exc
                continue
            except Exception:
                # Non-PlatformError — emit honest failure event and
                # bubble so the engine boundary can wrap.
                await self._emit_response_error(call_id=call_id, exc=None)
                raise

            await self._emit_response_success(call_id=call_id, response=response)
            return response

        # Loop fell through — every attempt raised a Recoverable
        # error. If the chain was empty (only primary attempted), the
        # patch's `llm.all_models_failed` ("after exhausting the
        # chain") doesn't apply — there was no chain. Re-raise the
        # original Recoverable so the outer engine emits
        # `thread.exchange_failed` with the unwrapped error as cause.
        # When the chain WAS non-empty, every entry's failure is
        # bundled into the load-bearing `attempts` field.
        if not self._chain and previous_error is not None:
            raise previous_error
        raise LlmAllModelsFailed(
            primary={
                "provider": self._primary_spec.provider,
                "model": self._primary_spec.model,
            },
            attempts=attempts,
            reason=(
                f"All {len(attempts)} model attempts failed with Recoverable errors"
            ),
        )

    # ---- Streaming-boundary helper ----------------------------------------

    def _stream_emitted_content(self) -> bool:
        """Return True iff the engine's streaming layer marked
        content as emitted before the current attempt failed. Phase
        1.3 patch §2.3: once tokens have streamed to the user,
        fallback does NOT apply."""
        if self._stream_state is None:
            return False
        return self._stream_state.has_emitted_content()

    # ---- Emission helpers -------------------------------------------------

    async def _emit_request(
        self,
        *,
        call_id: AgentCallId,
        spec: ModelSpec,
        request: LLMRequest,
    ) -> None:
        # Full ModelSpec dump (parameters + fallback) lands on the
        # request event — matches the pre-Phase-1.3 observer
        # behaviour. The `from`/`to` fields on `model.fallback` are
        # the canonical (provider, model) pair per patch §3.1.
        await self._emit(
            type_=EventType.MODEL_REQUEST,
            data=ModelRequestData(
                call_id=call_id,
                model=spec.model_dump(mode="json"),
                config_event_id=EventId(self._config_event_id or "ev_pending"),
                messages=[m.model_dump(mode="json") for m in request.messages],
            ).model_dump(mode="json"),
            actor=Actor(kind="agent", id=self._agent_id),
        )

    async def _emit_response_success(
        self,
        *,
        call_id: AgentCallId,
        response: LLMResponse,
    ) -> None:
        content_dict: dict[str, Any]
        if response.tool_calls:
            content_dict = {"tool_calls": list(response.tool_calls)}
            stop_reason = "tool_use"
        else:
            content_dict = {"text": response.content or ""}
            stop_reason = "text"
        await self._emit(
            type_=EventType.MODEL_RESPONSE,
            data=ModelResponseData(
                call_id=call_id,
                content=content_dict,
                usage=dict(response.usage),
                cost=0.0,
                latency_ms=0,
                stop_reason=stop_reason,
            ).model_dump(mode="json"),
            actor=Actor(kind="agent", id=self._agent_id),
        )

    async def _emit_response_error(
        self,
        *,
        call_id: AgentCallId,
        exc: PlatformError | None,
    ) -> None:
        err = Error.from_platform_error(exc) if exc is not None else None
        await self._emit(
            type_=EventType.MODEL_RESPONSE,
            data=ModelResponseData(
                call_id=call_id,
                content={},
                usage={},
                cost=0.0,
                latency_ms=0,
                stop_reason="error",
                error=err,
            ).model_dump(mode="json"),
            actor=Actor(kind="agent", id=self._agent_id),
        )

    async def _emit_fallback(
        self,
        *,
        from_spec: ModelSpec,
        to_spec: ModelSpec,
        previous_error: PlatformError,
        attempt: int,
    ) -> None:
        data = ModelFallbackData(
            from_model={
                "provider": from_spec.provider,
                "model": from_spec.model,
            },
            to_model={
                "provider": to_spec.provider,
                "model": to_spec.model,
            },
            reason=previous_error.code,
            reason_detail=str(previous_error),
            attempt=attempt,
        )
        await self._emit(
            type_=EventType.MODEL_FALLBACK,
            data=data.model_dump(mode="json", by_alias=True),
            actor=Actor(kind="system"),
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _attempt_entry(spec: ModelSpec, exc: PlatformError) -> dict[str, Any]:
    """One row in :attr:`LlmAllModelsFailed.detail.attempts`."""
    return {
        "model": {"provider": spec.provider, "model": spec.model},
        "error_code": exc.code,
        "detail": dict(exc.detail),
    }


# Module-level re-exports keep the public surface obvious. The six
# model.* PlatformErrors are re-exported so connector authors can
# `from engine._fallback import ModelRateLimited` rather than walking
# the errors package.
__all__ = [
    "FallbackLLMHandle",
    "ModelAuthenticationFailed",
    "ModelBadRequest",
    "ModelContextOverflow",
    "ModelRateLimited",
    "ModelServerError",
    "ModelTimeout",
    "StreamState",
    "build_fallback_chain",
    "is_fallback_triggering",
]
