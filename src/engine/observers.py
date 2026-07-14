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
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from engine._crewai_adapter import _tool_to_openai_spec
from errors import ExchangeCancelled, PlatformError, ToolUnauthorized
from schemas import (
    Actor,
    AgentCallId,
    Error,
    EventId,
    EventType,
    ModelRequestData,
    ModelResponseData,
    ToolCalledData,
    ToolFailedData,
    ToolResultData,
)
from schemas.events import ModelStreamingTokenData
from tools import ToolContext

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from engine.framework import LLMRequest, LLMResponse, ToolCall
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

        # AFTER — model.response.
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
        cancel: Any = None,
        register_span: Callable[[str, Any], None] | None = None,
        close_span: Callable[[str], None] | None = None,
    ) -> None:
        self._emit = emit
        self._agent_id = agent_id
        self._session_id = session_id
        self._thread_id = thread_id
        self._tools_subsystem = tools_subsystem
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
        tool_call_id = call.tool_call_id or f"tc_{uuid4()}"

        # BEFORE — tool.called.
        await self._emit(
            type_=EventType.TOOL_CALLED,
            data=ToolCalledData(
                tool_name=call.name,
                tool_call_id=tool_call_id,
                arguments=dict(call.arguments),
            ).model_dump(mode="json"),
            actor=Actor(kind="agent", id=self._agent_id),
        )

        # spec-068 Part A — register the span AFTER `tool.called` has
        # been appended so the registry can never fire a `tool.failed`
        # for a call the log never mentions. The factory closes over
        # `tool_call_id` + `call.name` so the exchange finally can
        # build a symmetric terminal payload without needing to
        # re-walk the log.
        if self._register_span is not None:
            span_id = tool_call_id
            tool_name = call.name

            def _terminal_factory() -> tuple[EventType, dict[str, Any], Actor]:
                return _build_session_interrupted_tool_failed(
                    tool_call_id=span_id,
                    tool_name=tool_name,
                )

            self._register_span(span_id, _terminal_factory)

        # spec-068 Part A — the outer `try/finally` guarantees that a
        # cancel firing between `tool.called` and any downstream
        # terminal still emits a `tool.failed(session.interrupted)`
        # BEFORE the exception propagates. The `ExchangeCancelled` /
        # `CancelledError` arm below is the observer's own belt; the
        # exchange finally is the outer suspenders (registry-based).
        # On every normal arm below we also `close_span` so the
        # exchange finally doesn't double-emit.
        try:
            try:
                result = await self._tools_subsystem.invoke_tool(
                    tool_name=call.name,
                    args=dict(call.arguments),
                    context=ToolContext(
                        agent_id=self._agent_id,  # type: ignore[arg-type]
                        session_id=self._session_id,  # type: ignore[arg-type]
                        thread_id=self._thread_id,  # type: ignore[arg-type]
                        principal=f"agent.{self._agent_id}",
                        secrets={},
                        tool_call_id=tool_call_id,
                        cancellation=self._cancel,
                    ),
                )
            except ToolUnauthorized as exc:
                err = Error.from_platform_error(exc)
                # Derive the exact `/perm allow` chat command from the
                # matched rule's `what` + the in-flight args. The model
                # and the chat renderer both see this hint; the user
                # types it to un-block.
                recovery_hint = _derive_authz_recovery_hint(
                    exc, arguments=call.arguments
                )
                await self._emit(
                    type_=EventType.TOOL_FAILED,
                    data=ToolFailedData(
                        tool_call_id=tool_call_id,
                        tool_name=call.name,
                        error=err,
                        recovery_hint=recovery_hint,
                    ).model_dump(mode="json"),
                    actor=Actor(kind="agent", id=self._agent_id),
                )
                # Diagnostic — return error payload so framework continues.
                payload = err.model_dump(mode="json")
                if recovery_hint is not None:
                    payload["recovery_hint"] = recovery_hint
                return {"error": payload}
            except PlatformError as exc:
                err = Error.from_platform_error(exc)
                await self._emit(
                    type_=EventType.TOOL_FAILED,
                    data=ToolFailedData(
                        tool_call_id=tool_call_id,
                        tool_name=call.name,
                        error=err,
                    ).model_dump(mode="json"),
                    actor=Actor(kind="agent", id=self._agent_id),
                )
                return {"error": err.model_dump(mode="json")}
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
                # path still runs.
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

            # AFTER — tool.result.
            result_payload = _normalize_result(result.result)
            await self._emit(
                type_=EventType.TOOL_RESULT,
                data=ToolResultData(
                    tool_call_id=tool_call_id,
                    tool_name=call.name,
                    result=result_payload,
                ).model_dump(mode="json"),
                actor=Actor(kind="agent", id=self._agent_id),
            )
            return result.result
        finally:
            # Every arm above (result, failed, cancel-with-emit) has
            # already appended a terminal for this span — mark it
            # closed so the exchange finally does not double-emit. If
            # the emit itself raised before appending (rare — the
            # store is durable-first), the span stays open and the
            # exchange finally picks it up.
            if self._close_span is not None:
                self._close_span(tool_call_id)


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


def _normalize_result(value: object) -> dict[str, Any]:
    """Wrap a tool result for the event-data dict shape.

    The event payload `tool.result.data.result` is a dict. Dict
    results pass through; everything else is wrapped under a `value`
    key so the verbatim content remains capturable.
    """
    if isinstance(value, dict):
        return value
    return {"value": value}


# ---------------------------------------------------------------------------
# Denied-recovery hint — derives the `/perm allow` slash command
# ---------------------------------------------------------------------------


_TARGET_ARG_PRIORITY = ("path", "command", "url")
"""Same order as `tools.dispatch._TARGET_ARG_PRIORITY`. Duplicated
here (rather than imported) because the observer can't take a
dependency on the dispatcher's internal helper — both modules read
the convention from the same contract."""


def _derive_authz_recovery_hint(
    exc: ToolUnauthorized,
    *,
    arguments: dict[str, Any] | None,
) -> str | None:
    """Build the `/perm allow <tool> <pattern>` hint string.

    Reads `ToolUnauthorized.detail`'s `matched_rule` to find the
    qualified tool ref (or falls back to the legacy action namespace
    `tool.<name>` when the matched rule's `what` is in old shape).
    The suggested pattern is derived from the in-flight arguments
    via the same convention the modal uses (first whitespace-bounded
    token for bash; parent dir for file tools; `*` otherwise).
    Returns `None` if there's nothing actionable to surface (e.g.
    no rule matched — pure default deny — in which case the user
    can use `/perm allow <ref> *`).

    The pattern is intentionally **broad** by default (`*`); the
    user can edit before submitting. The point of the hint is to
    save them from looking up the qualified tool ref and the
    slash-command grammar — not to guess their intent precisely.
    """
    detail = exc.detail or {}
    tool_name = detail.get("tool_name") or ""
    matched_rule = detail.get("matched_rule") or {}
    if not isinstance(tool_name, str) or not tool_name:
        return None

    # Prefer the matched rule's `what` when it's the new
    # qualified-ref shape (contains `/`); fall back to a legacy
    # `tool.<name>` action namespace and ultimately to the bare
    # tool name. Bare-name fallback isn't a valid /perm tool arg
    # — we suppress the hint so we never tell the user to type
    # `/perm allow bash` (which would fail validation).
    what = matched_rule.get("what") if isinstance(matched_rule, dict) else None
    tool_ref = what if isinstance(what, str) and "/" in what else None

    if tool_ref is None:
        return None

    pattern = _suggest_glob_pattern(tool_name=tool_name, arguments=arguments)
    # Quote when the pattern contains whitespace so the slash
    # parser splits the verb / ref / pattern correctly.
    quoted = f'"{pattern}"' if " " in pattern else pattern
    return f"/perm allow {tool_ref} {quoted}"


def _suggest_glob_pattern(
    *,
    tool_name: str,
    arguments: dict[str, Any] | None,
) -> str:
    """Mirror the chat modal's `suggest_pattern` helper.

    Same three heuristics in priority order:

    1. bash with a command target: first whitespace-bounded token
       → `"<token> *"`.
    2. File tools with a path target: parent-dir glob
       `"<parent>/*"`.
    3. Fallback: `"*"`.

    Kept independent of the modal so the engine can run without
    importing chat code (the engine doesn't depend on `cli/`).
    """
    if not arguments:
        return "*"
    target: str | None = None
    for attr in _TARGET_ARG_PRIORITY:
        value = arguments.get(attr)
        if isinstance(value, str):
            target = value
            break
    if target is None:
        return "*"
    if tool_name == "bash":
        first_token = target.strip().split(maxsplit=1)[0] if target.strip() else ""
        if first_token:
            return f"{first_token} *"
    if tool_name in {"read_file", "write_file", "list_files"}:
        parent, sep, _name = target.rpartition("/")
        if sep:
            return f"{parent}/*"
    return "*"
