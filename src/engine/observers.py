"""Engine seam wrappers — the in-band observation path.

The two wrappers implement the Framework Protocol's
[`LLMHandle`][engine.framework.LLMHandle] and
[`ToolHandle`][engine.framework.ToolHandle]. The engine constructs
them per exchange and hands them to `Framework.run_exchange`; the
framework invokes them; the engine captures events around each call.

Why in-band: CrewAI's event bus dispatches handlers off-thread with
no back-pressure and silently swallows handler exceptions — unsafe
as the primary observation path. Going in-band (subclass
BaseLLM-equivalent, wrap tools) keeps event ordering deterministic
and serial inside the agent's own async context.

These classes are engine-internal — not part of the public surface.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, assert_never
from uuid import uuid4

from engine._crewai_adapter import _tool_to_openai_spec
from engine._think_strip import strip_leading_think
from errors import ExchangeCancelled, PlatformError
from schemas import (
    Actor,
    AgentCallId,
    Error,
    EventId,
    EventType,
    ModelRequestData,
    ModelResponseData,
    ToolFailedData,
)
from schemas.events import ModelStreamingTokenData
from tools.observed import Denied, Failed, Ok, invoke_observed

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from engine.framework import LLMRequest, LLMResponse, ToolCall
    from schemas import CallerChain
    from tools.decorator import ToolDefinition


_log = logging.getLogger("engine.observers")


# spec-068 Part A — the same terminal-payload shape the renderer's
# `EventAdapter._append_synthetic_tool_failed` uses today. Part B
# will strip the renderer's durable write and the resume-time
# reconciliation will emit the SAME string, so keep the constants
# here so both call sites reference one source of truth.
_SESSION_INTERRUPTED_ERROR_TYPE = "session.interrupted"
_SESSION_INTERRUPTED_MESSAGE = (
    "The chat session ended while this tool was awaiting user input. Retry to continue."
)


class _ObservedLLMHandle:
    """Engine-controlled LLMHandle wrapping a constructor-supplied LLM.

    Implements [`LLMHandle`][engine.framework.LLMHandle]. On each
    call:

    1. Generate a fresh `cl_<uuid>` call id.
    2. Emit `model.request` BEFORE invoking the underlying LLM. The
       event data carries `call_id`, `model`, `config_event_id` —
       the assembled context is derivable from the path.
    3. Invoke the underlying LLM callable (the engine's
       constructor-supplied `llm`).
    4. On exception: emit `model.response` with `stop_reason="error"`
       carrying the Error envelope, then re-raise so the engine's
       retry / Fatal layer can decide. Bubble PlatformErrors
       verbatim; wrap everything else as `PlatformInternalError` at
       the engine boundary.
    5. On success: emit `model.response` AFTER and return the
       [`LLMResponse`][engine.framework.LLMResponse] unchanged.

    Materialization holds at this seam because the
    [`LLMRequest`][engine.framework.LLMRequest] is what the LLM
    actually saw (post-framework-wrap).
    """

    def __init__(
        self,
        *,
        emit: Callable[..., Awaitable[None]],
        agent_id: str,
        model_spec: dict[str, Any],
        config_event_id: str | None,
        llm: Callable[[LLMRequest], Awaitable[LLMResponse]],
    ) -> None:
        self._emit = emit
        self._agent_id = agent_id
        self._model_spec = model_spec
        self._config_event_id = config_event_id
        self._llm = llm

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        call_id = AgentCallId(f"cl_{uuid4()}")
        # BEFORE — model.request. Normalise the tool advertisements to
        # the OpenAI spec envelope (`{type: function, function: {name,
        # description, parameters}}`) before emitting so the event log
        # records what actually crosses the LLM wire, not the engine's
        # internal `{name, description, schema}` shape. Both the
        # observer and `_crewai_adapter.call` invoke
        # `_tool_to_openai_spec`; it is idempotent, so the double call
        # is a no-op on the second pass.
        request_data = ModelRequestData(
            call_id=call_id,
            model=dict(self._model_spec),
            config_event_id=EventId(self._config_event_id or "ev_pending"),
            messages=[m.model_dump(mode="json") for m in request.messages],
            tools=[_tool_to_openai_spec(t) for t in request.tools],
        )
        await self._emit(
            type_=EventType.MODEL_REQUEST,
            data=request_data.model_dump(mode="json"),
            actor=Actor(kind="agent", id=self._agent_id),
        )

        # Streaming hook: if the wrapped LLM callable exposes a
        # `stream` async method taking (request, on_delta), we call
        # it and emit `MODEL_STREAMING_TOKEN` events per chunk.
        # `on_delta` is a coroutine the observer supplies that emits
        # the event. Non-streaming providers stay on the non-streaming
        # `__call__` path — behaviour parity with today's flow.
        stream_method = getattr(self._llm, "stream", None)
        response: LLMResponse
        try:
            if callable(stream_method):

                async def _on_delta(text_delta: str) -> None:
                    await self._emit(
                        type_=EventType.MODEL_STREAMING_TOKEN,
                        data=ModelStreamingTokenData(
                            call_id=call_id,
                            text_delta=text_delta,
                        ).model_dump(mode="json"),
                        actor=Actor(kind="agent", id=self._agent_id),
                    )

                response = await stream_method(request, _on_delta)
            else:
                response = await self._llm(request)
        except PlatformError as exc:
            err = Error.from_platform_error(exc)
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
            raise
        except Exception:
            # Non-platform exception — let the engine boundary wrap.
            # Emit a failure response first so the log is honest.
            await self._emit(
                type_=EventType.MODEL_RESPONSE,
                data=ModelResponseData(
                    call_id=call_id,
                    content={},
                    usage={},
                    cost=0.0,
                    latency_ms=0,
                    stop_reason="error",
                    error=None,
                ).model_dump(mode="json"),
                actor=Actor(kind="agent", id=self._agent_id),
            )
            raise

        # bug-087 — strip a leading `<think>…</think>` block from
        # inline-reasoning models so both the emitted `model.response`
        # event content and the returned `LLMResponse.content` are
        # clean; the reasoning is captured on `reasoning_trace`. A
        # future streaming connector would need delta-aware stripping
        # (deltas bypass this seam).
        clean, reasoning = strip_leading_think(response.content)
        if reasoning is not None:
            response = response.model_copy(update={"content": clean})

        # AFTER — model.response.
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
        return response


class _ObservedToolHandle:
    """Engine-controlled ToolHandle wrapping the Tools subsystem.

    Implements [`ToolHandle`][engine.framework.ToolHandle]. Routes
    the tool call through `ToolsSubsystem.invoke_tool`, which itself
    routes through authz → secrets → sandbox → execute. An
    in-exchange tool failure (Diagnostic) MUST NOT raise — the
    wrapper returns an error payload to the framework so the model
    sees the failure on the next turn.
    """

    def __init__(
        self,
        *,
        emit: Callable[..., Awaitable[None]],
        agent_id: str,
        session_id: str,
        thread_id: str,
        tools_subsystem: Any,
        defn: ToolDefinition,
        caller_chain: CallerChain,
        cancel: Any = None,
        register_span: Callable[[str, Any], None] | None = None,
        close_span: Callable[[str], None] | None = None,
    ) -> None:
        self._emit = emit
        self._agent_id = agent_id
        self._session_id = session_id
        self._thread_id = thread_id
        self._tools_subsystem = tools_subsystem
        # bug-158: the exchange's caller chain, forwarded into every
        # call's `ToolContext` so `from:`-scoped rules match.
        self._caller_chain = caller_chain
        # The exchange's CancelToken, threaded by the engine.
        # Forwarded into the ToolContext at invocation time so tool
        # bodies can race long work against
        # `ctx.cancellation.wait_cancelled()`. `None` when the
        # engine constructed the observer without cancellation
        # (tests, programmatic paths).
        self._cancel = cancel
        # spec-068 Part A — span-registry seams. The engine's
        # `_Exchange` passes bound `register_span` / `close_span`
        # methods; test / programmatic constructions leave them
        # `None` and the observer degrades gracefully (no registry
        # bookkeeping, but every emit arm still fires as before).
        # The engine keeps a per-exchange OrderedDict keyed by
        # `tool_call_id`; the observer's own `try/finally` is the
        # primary guarantee for a paired terminal on cancel — the
        # registry is the exchange-level backstop.
        self._register_span = register_span
        self._close_span = close_span
        # Public attributes required by the ToolHandle Protocol.
        self.name = defn.name
        # bug-024: expose the tool's rich `@tool(description=...)`
        # copy so `MinimalFramework.run_exchange` can advertise it to
        # the LLM. Without this attribute the model saw every tool as
        # `"Tool: <name>"` and Kimi K2.6 (the workspace default)
        # fell back to `bash` whenever a workflow ref was requested.
        self.description = defn.description
        self.schema = defn.args_schema.model_json_schema()

    async def __call__(self, call: ToolCall) -> Any:
        # spec-119 — the emit + invoke + happy/denied/errored arms
        # live in the caller-agnostic core `tools.observed.
        # invoke_observed`; this wrapper keeps everything that is
        # genuinely exchange-coupled: the span registry and the
        # cancel terminal. The interposed emit below threads the two
        # facts the wrapper needs back out of the core WITHOUT the
        # core knowing about spans or cancellation:
        #
        # - `tool_call_id`: the id the core minted (it owns the
        #   `call.tool_call_id or f"tc_{uuid4()}"` fallback). Captured
        #   AFTER the `tool.called` append returns — which is also
        #   the spec-068 registration point, so the registry can
        #   never fire a `tool.failed` for a call the log never
        #   mentions.
        # - `in_terminal_emit`: flipped BEFORE awaiting a
        #   `tool.result`/`tool.failed` append. A cancel escaping
        #   during that emit did NOT come from `invoke_tool`, so the
        #   cancel arm must not write a second terminal — identical
        #   to the pre-split method, whose cancel `except` wrapped
        #   only the `invoke_tool` call.
        tool_call_id: str | None = None
        in_terminal_emit = False

        async def _emit_and_register(
            *,
            type_: EventType,
            data: dict[str, Any],
            actor: Actor,
        ) -> None:
            nonlocal in_terminal_emit, tool_call_id
            if type_ is not EventType.TOOL_CALLED:
                in_terminal_emit = True
            await self._emit(type_=type_, data=data, actor=actor)
            if type_ is not EventType.TOOL_CALLED:
                return
            tool_call_id = str(data["tool_call_id"])
            # spec-068 Part A — register the span AFTER `tool.called`
            # has been appended. The factory closes over the id +
            # `call.name` so the exchange finally can build a
            # symmetric terminal payload without re-walking the log.
            if self._register_span is not None:
                span_id = tool_call_id
                tool_name = call.name

                def _terminal_factory() -> tuple[EventType, dict[str, Any], Actor]:
                    return _build_session_interrupted_tool_failed(
                        tool_call_id=span_id,
                        tool_name=tool_name,
                    )

                self._register_span(span_id, _terminal_factory)

        # spec-068 Part A — the `try/finally` guarantees a cancel
        # firing between `tool.called` and any downstream terminal
        # still emits a `tool.failed(session.interrupted)` BEFORE the
        # exception propagates. The `ExchangeCancelled` /
        # `CancelledError` arm below is the observer's own belt; the
        # exchange finally is the outer suspenders (registry-based).
        # On every normal arm the core has already appended a
        # terminal, and the `finally` `close_span`s so the exchange
        # finally doesn't double-emit.
        try:
            outcome = await invoke_observed(
                emit=_emit_and_register,
                agent_id=self._agent_id,
                session_id=self._session_id,
                thread_id=self._thread_id,
                tools_subsystem=self._tools_subsystem,
                tool_name=call.name,
                args=call.arguments,
                caller_chain=self._caller_chain,
                tool_call_id=call.tool_call_id,
                cancel=self._cancel,
            )
        except (ExchangeCancelled, asyncio.CancelledError):
            # spec-068 Part A — cancel between `tool.called` and
            # `tool.result`/`tool.failed` used to leave the span
            # dangling; downstream assembly then permanently
            # 400'd the session ("tool_call_ids did not have
            # response messages"). Emit the same synthetic
            # terminal shape `EventAdapter._append_synthetic_
            # tool_failed` writes today (Part B rips out the
            # renderer's durable write in favour of engine
            # emission), then re-raise so the exchange failure
            # path still runs. Skipped when `tool.called` never
            # landed (nothing to pair) or when the cancel escaped
            # from a terminal emit (a terminal may already be
            # appended) — both identical to the pre-split method.
            if tool_call_id is not None and not in_terminal_emit:
                type_, data, actor = _build_session_interrupted_tool_failed(
                    tool_call_id=tool_call_id,
                    tool_name=call.name,
                )
                try:
                    await self._emit(type_=type_, data=data, actor=actor)
                except Exception:  # noqa: BLE001 — emit best-effort on cancel
                    # Swallow after logging and re-raise the original
                    # cancel — priority is a clean cancel bubble, not
                    # a second exception on top. Note: the outer
                    # `finally` below always calls `close_span`, so
                    # the exchange finally's registry backstop cannot
                    # retry this emit either — the span is gone from
                    # the registry by the time the exchange finally
                    # runs. An emit failure here means the store
                    # append_event itself failed, so the log is
                    # unreachable in this process regardless; the
                    # true safety net for that failure mode is Part
                    # B's resume reconciliation, which reads the log
                    # on the next successful boot and appends any
                    # still-missing terminal via `_emit`.
                    _log.exception(
                        "observer: session-interrupted terminal emit "
                        "failed for tool_call_id=%s",
                        tool_call_id,
                    )
            raise
        finally:
            # Every completed arm (result, failed, cancel-with-emit)
            # has already appended a terminal for this span — mark it
            # closed so the exchange finally does not double-emit. If
            # an emit raised before appending (rare — the store is
            # durable-first), either the span was never registered
            # (`tool.called` emit failed → id is None) or it stays
            # open and the exchange finally picks it up.
            if self._close_span is not None and tool_call_id is not None:
                self._close_span(tool_call_id)

        # Outcome → the framework's return shape. Raw result on `Ok`
        # (the emit-normalized / return-raw split is the core's
        # contract); Diagnostic error payload on `Denied`/`Failed` so
        # the framework continues and the model sees the failure —
        # WITH `recovery_hint` forwarded (this engine mapper is the
        # surface where the hint is deliberately model-visible; an
        # external bridge mapper drops it instead).
        match outcome:
            case Ok(result=raw_result):
                return raw_result
            case (
                Denied(error=error, recovery_hint=recovery_hint)
                | Failed(error=error, recovery_hint=recovery_hint)
            ):
                payload = error.model_dump(mode="json")
                if recovery_hint is not None:
                    payload["recovery_hint"] = recovery_hint
                return {"error": payload}
            case _:
                assert_never(outcome)


def _build_session_interrupted_tool_failed(
    *,
    tool_call_id: str,
    tool_name: str,
) -> tuple[EventType, dict[str, Any], Actor]:
    """Build a `tool.failed(session.interrupted)` terminal payload.

    Central factory so the observer's cancel arm and the exchange
    finally's registry-close both emit byte-identical events. The
    shape mirrors the renderer's existing
    `EventAdapter._append_synthetic_tool_failed` write (same
    `error.type` string, same `error.message`) so Part B — which
    rips out the renderer's durable write in favour of engine-side
    reconciliation on resume — can adopt this factory without any
    consumer-visible diff.

    Actor is `system` (not `agent`): the terminal is engine-authored
    on behalf of the cancelled exchange, not a model-attributable
    event. Consumers filter on this to distinguish forced closure
    from a model-emitted failure.
    """
    error = Error(
        type=_SESSION_INTERRUPTED_ERROR_TYPE,
        message=_SESSION_INTERRUPTED_MESSAGE,
        where="engine.observers._ObservedToolHandle",
    )
    data = ToolFailedData(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        error=error,
    ).model_dump(mode="json")
    return EventType.TOOL_FAILED, data, Actor(kind="system")


# spec-119: `_normalize_result`, `_derive_authz_recovery_hint`, and
# `_suggest_glob_pattern` moved to `tools.observed` — the invocation
# core builds the `tool.result`/`tool.failed` event data, and a
# `tools/`-side module cannot import helpers from `engine/` without
# an import cycle.
