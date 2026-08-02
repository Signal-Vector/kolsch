"""Engine fallback orchestrator — primary → chain → exhaustion.

Three surfaces live here:

- [`is_fallback_triggering`][engine._fallback.is_fallback_triggering]
  — the predicate the engine consults to decide whether a
  Recoverable `model.*` error should walk to the next chain entry.
  Pure and exhaustive over the catalog's `model.*` codes;
  import-time validation raises `ImportError` if a new `model.*`
  code lands without a classification.
- [`build_fallback_chain`][engine._fallback.build_fallback_chain] —
  resolves and validates an agent's fallback chain at agent-load
  time. Each entry's provider must appear in `llm_providers`;
  duplicates emit
  [`ConfigFallbackDuplicate`][errors.ConfigFallbackDuplicate]
  (Diagnostic) warnings rather than errors. Returns
  `list[ModelSpec]`; clients are NOT built here (lazy construction
  is the engine loop's job).
- [`FallbackLLMHandle`][engine._fallback.FallbackLLMHandle] — the
  framework-facing LLMHandle wrapping the per-attempt observation
  plus the chain loop: try primary → on Recoverable, emit
  `model.fallback` and advance → on Fatal, abort → on exhaustion,
  raise [`LlmAllModelsFailed`][errors.LlmAllModelsFailed].

The orchestrator does not remember the chain across exchanges —
each exchange starts from the primary. Sticky preferences are a
later concern.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from engine._crewai_adapter import _tool_to_openai_spec
from engine._think_strip import strip_leading_think
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
# `_FATAL_MODEL_CODES` causes `_assert_classification_exhaustive` to
# fail at import time — the AGENTS.md hard rule enforced in code.
_RECOVERABLE_MODEL_CODES: frozenset[str] = frozenset(
    {
        "model.rate_limited",
        "model.server_error",
        "model.timeout",
        "model.context_overflow",
        # Generic upstream failure, kept Recoverable in the catalog
        # and treated as fallback-triggering for back-compat.
        "model.upstream_error",
        # Engine-side parse failure. Recoverable per the catalog;
        # the chain may have a more reliable model.
        "model.malformed_response",
    }
)

_FATAL_MODEL_CODES: frozenset[str] = frozenset(
    {
        "model.authentication_failed",
        "model.bad_request",
        # Chain won't help — we already retried.
        "model.retry_exhausted",
        # A native connector's local CLI (e.g. `claude`) is missing or
        # logged out. A sibling model routed through the same binary
        # would fail identically, so advancing the chain is pointless —
        # the operator must install / log in (spec-118).
        "model.binary_unavailable",
    }
)


def is_fallback_triggering(err: PlatformError) -> bool:
    """Return True iff the engine should advance the fallback chain.

    Behaviour summary:

    - `err.code` in `_RECOVERABLE_MODEL_CODES` → True.
    - `err.code` in `_FATAL_MODEL_CODES` → False.
    - Anything not in `model.*` → False; fallback is about model
      availability, not engine bugs.
    - A `model.*` code not in either set → import-time `ImportError`
      ensures this never happens at runtime (see
      `_assert_classification_exhaustive`).
    """
    code = err.code
    if code in _RECOVERABLE_MODEL_CODES:
        return True
    if code in _FATAL_MODEL_CODES:
        return False
    # Non-model.* — never triggers fallback.
    return False


def _assert_classification_exhaustive() -> None:
    """Import-time guard — every `model.*` code must be classified.

    Walks the registered catalog; any `model.*` code that is neither
    in `_RECOVERABLE_MODEL_CODES` nor `_FATAL_MODEL_CODES` raises
    `ImportError` naming the unclassified code. The fix is to
    declare the code's level in `errors.fallback` (or its sibling
    module) AND add it to the classification set here in the SAME
    change.
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
            f"docs/explanation/error-model.md: {sorted(unclassified)}"
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

    Each entry's `provider` must be in `configured_providers`; if
    not, raises
    [`LlmProviderNotConfigured`][errors.LlmProviderNotConfigured]
    naming the offending entry position. Duplicates (either against
    the primary or another entry) are allowed but emit
    [`ConfigFallbackDuplicate`][errors.ConfigFallbackDuplicate]
    (Diagnostic). `emit_warning` is the surface — production wires
    it to the CLI banner; tests collect to a list. The list is
    returned as-is.

    Does NOT build `crewai.LLM` clients — that is the engine loop's
    job at use time.

    Args:
        agent_id: Stamped on
            [`ConfigFallbackDuplicate`][errors.ConfigFallbackDuplicate]
            warnings so the user knows which agent emitted them.
        fallback_specs: The agent's declared fallback chain entries.
        primary: The agent's primary `ModelSpec` — included in
            duplicate detection so a fallback entry equal to the
            primary surfaces.
        configured_providers: Providers actually wired in
            `platform.yaml`.
        emit_warning: Sink for Diagnostic duplicate warnings.

    Returns:
        The validated chain — same entries, same order.

    Raises:
        LlmProviderNotConfigured: A chain entry's provider is not
            wired.
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
# Streaming boundary
# ---------------------------------------------------------------------------


class StreamState:
    """Per-call streaming-boundary signal.

    Carries one boolean: has the connector emitted any content
    token to the user yet? The
    [`FallbackLLMHandle`][engine._fallback.FallbackLLMHandle]
    Recoverable branch consults this on every retry attempt. Once
    the flag is set, fallback is short-circuited — partial output
    has been committed to the log and the user's terminal; swapping
    models mid-stream would lie about which model produced the
    answer.

    Wiring:

    - The engine constructs one `StreamState` per `execute_stream`
      call and passes it to the handle.
    - The connector's streaming layer calls `mark_content_emitted`
      on the first content chunk.
    - The handle's orchestrator polls `has_emitted_content` before
      advancing on a Recoverable error; if True, it re-raises the
      original error without emitting a `model.fallback` event.

    Non-streaming callers don't need this — they pass `None` and
    the orchestrator behaves exactly as before. The current
    connectors do not stream through the handle (they return one
    `LLMResponse`); when a streaming connector lands, it sets this
    flag on the first token. The contract is front-loaded so
    connector authors implement against a real seam.
    """

    __slots__ = ("_emitted",)

    def __init__(self) -> None:
        self._emitted = False

    def mark_content_emitted(self) -> None:
        """Set the flag. Idempotent."""
        self._emitted = True

    def has_emitted_content(self) -> bool:
        """Return True if any content token has been emitted."""
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
    """Engine-controlled LLMHandle implementing the fallback loop.

    Each `__call__(request)` walks the chain:

    1. Try primary → on success, emit `model.request` /
       `model.response` and return.
    2. On Recoverable `model.*`: emit a failure `model.response`
       (`stop_reason=error`), then a `model.fallback` event, then
       re-issue against the next chain entry's lazily-built
       LLMCallable. Repeat.
    3. On Fatal `model.*`: emit the failure `model.response` and
       re-raise the original `PlatformError`. The chain is NOT
       consulted.
    4. On chain exhaustion: raise
       [`LlmAllModelsFailed`][errors.LlmAllModelsFailed] with the
       full attempts list.

    Streaming is supported by the engine's outer layer; this handle
    does not currently distinguish stream-pre-token-emitted from
    stream-post-token-emitted (the engine sets
    `stream_pre_first_token` on the request context to gate
    fallback at the outer level — see the engine's streaming path).
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
        # Populated by the engine's streaming path; None for
        # non-streaming exchanges. When set, the orchestrator's
        # Recoverable branch consults
        # `stream_state.has_emitted_content()` before advancing.
        self._stream_state = stream_state
        # Production callers can observe `factory_calls` to confirm
        # lazy-build behaviour.
        self.factory_calls: list[ModelSpec] = []

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        all_specs = [self._primary_spec, *self._chain]
        attempts: list[dict[str, Any]] = []
        previous_spec: ModelSpec | None = None
        previous_error: PlatformError | None = None

        for attempt_idx, spec in enumerate(all_specs):
            # Emit `model.fallback` BEFORE the next attempt's
            # `model.request` (ordering: from/to pair sits between
            # the failed response and the next request).
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
                # for the chain entries — a fallback to an
                # unconfigured provider should have failed at agent
                # load, so surfacing here means the chain is
                # mis-resolved.
                if not is_fallback_triggering(exc):
                    raise
                if self._stream_emitted_content():
                    # Streaming boundary — partial output already
                    # committed; abort with the original error
                    # rather than swapping models mid-stream.
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
                    # Streaming boundary — post-token-emitted
                    # Recoverable does NOT advance; re-raise the
                    # original Recoverable so the engine emits
                    # `thread.exchange_failed` with the truncation
                    # visible.
                    raise
                previous_spec = spec
                previous_error = exc
                continue
            except Exception:
                # Non-PlatformError — emit honest failure event and
                # bubble so the engine boundary can wrap.
                await self._emit_response_error(call_id=call_id, exc=None)
                raise

            # bug-087 — strip a leading `<think>…</think>` block from
            # inline-reasoning models (e.g. openrouter minimax-m3) so
            # both the emitted `model.response` event content and the
            # returned `LLMResponse.content` are clean. The reasoning
            # is preserved on the event's `reasoning_trace` field.
            clean, reasoning = strip_leading_think(response.content)
            if reasoning is not None:
                response = response.model_copy(update={"content": clean})
            await self._emit_response_success(
                call_id=call_id, response=response, reasoning=reasoning
            )
            return response

        # Loop fell through — every attempt raised a Recoverable
        # error. If the chain was empty (only primary attempted),
        # `llm.all_models_failed` ("after exhausting the chain")
        # doesn't apply — there was no chain. Re-raise the original
        # Recoverable so the outer engine emits
        # `thread.exchange_failed` with the unwrapped error as
        # cause. When the chain WAS non-empty, every entry's
        # failure is bundled into the load-bearing `attempts` field.
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
        """Return True iff streaming has emitted any content.

        Once tokens have streamed to the user, fallback does NOT
        apply.
        """
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
        # request event. The `from` / `to` fields on
        # `model.fallback` are the canonical (provider, model) pair.
        #
        # Also include the tool-schema list the framework forwarded,
        # normalised to the OpenAI spec envelope so the recorded
        # event matches what actually crosses the LLM wire (not the
        # engine's internal `{name, description, schema}` shape). The
        # observer at `_ObservedLLMHandle` uses the same normalisation;
        # both invocations are idempotent via `_tool_to_openai_spec`'s
        # `"function" in tool` guard.
        await self._emit(
            type_=EventType.MODEL_REQUEST,
            data=ModelRequestData(
                call_id=call_id,
                model=spec.model_dump(mode="json"),
                config_event_id=EventId(self._config_event_id or "ev_pending"),
                messages=[m.model_dump(mode="json") for m in request.messages],
                tools=[_tool_to_openai_spec(t) for t in request.tools],
            ).model_dump(mode="json"),
            actor=Actor(kind="agent", id=self._agent_id),
        )

    async def _emit_response_success(
        self,
        *,
        call_id: AgentCallId,
        response: LLMResponse,
        reasoning: str | None = None,
    ) -> None:
        content_dict: dict[str, Any]
        if response.tool_calls:
            content_dict = {"tool_calls": list(response.tool_calls)}
            stop_reason = "tool_use"
        else:
            content_dict = {"text": response.content or ""}
            stop_reason = "text"
        reasoning_trace = {"text": reasoning} if reasoning is not None else None
        await self._emit(
            type_=EventType.MODEL_RESPONSE,
            data=ModelResponseData(
                call_id=call_id,
                content=content_dict,
                reasoning_trace=reasoning_trace,
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
    """One row in `LlmAllModelsFailed.detail.attempts`."""
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
