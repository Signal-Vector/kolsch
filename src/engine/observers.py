"""Engine seam wrappers — the in-band observation path.

The two wrappers below implement the Framework Protocol's
:class:`LLMHandle` and :class:`ToolHandle` Protocols. The engine
constructs them per exchange and hands them to
:meth:`Framework.run_exchange`; the framework invokes them; the engine
captures events around each call.

Why in-band: per the prereq design note (§2.5), CrewAI's event bus
dispatches handlers off-thread with no back-pressure and silently
swallows handler exceptions — unsafe as the primary observation path.
Going in-band (subclass BaseLLM-equivalent, wrap tools) keeps event
ordering deterministic and serial inside the agent's own async
context.

These classes are engine-internal — not part of the public surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from errors import PlatformError, ToolUnauthorized
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
from tools import ToolContext

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from engine.framework import LLMRequest, LLMResponse, ToolCall
    from tools.decorator import ToolDefinition


class _ObservedLLMHandle:
    """Engine-controlled LLMHandle. Implements
    :class:`engine.framework.LLMHandle`.

    Flow on each call:
    1. Generate a fresh ``cl_<uuid>`` call id.
    2. Emit ``model.request`` BEFORE invoking the underlying LLM.
       Per Schemas §B.8 the event data carries ``call_id``, ``model``,
       ``config_event_id`` only — the assembled context is derivable
       from the path per §B.5.
    3. Invoke the underlying LLM callable (the engine's constructor-
       supplied ``llm``).
    4. On exception: emit ``model.response`` with stop_reason="error"
       carrying the Error envelope, then re-raise so the engine's
       retry/Fatal layer can decide. Bubble PlatformErrors verbatim;
       wrap everything else as :class:`PlatformInternalError` at
       the engine boundary.
    5. On success: emit ``model.response`` AFTER and return the
       :class:`LLMResponse` unchanged.

    Materialization (I3) holds at this seam because the ``LLMRequest``
    is what the LLM actually saw (post-framework-wrap, per the §B.6
    draft-0.5 amendment).
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
        # BEFORE — model.request (Engine I6).
        request_data = ModelRequestData(
            call_id=call_id,
            model=dict(self._model_spec),
            config_event_id=EventId(self._config_event_id or "ev_pending"),
            messages=[m.model_dump(mode="json") for m in request.messages],
        )
        await self._emit(
            type_=EventType.MODEL_REQUEST,
            data=request_data.model_dump(mode="json"),
            actor=Actor(kind="agent", id=self._agent_id),
        )

        try:
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

        # AFTER — model.response (Engine I6).
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
    """Engine-controlled ToolHandle. Implements
    :class:`engine.framework.ToolHandle`.

    Routes the tool call through :meth:`ToolsSubsystem.invoke_tool`,
    which itself routes through authz → secrets → sandbox → execute
    (Engine I9 sandbox + I10 secrets). Per spec §1.6, an in-exchange
    tool failure (Diagnostic) MUST NOT raise — the wrapper returns
    an error payload to the framework so the model sees the failure
    on the next turn.
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
    ) -> None:
        self._emit = emit
        self._agent_id = agent_id
        self._session_id = session_id
        self._thread_id = thread_id
        self._tools_subsystem = tools_subsystem
        # Public attributes required by the ToolHandle Protocol.
        self.name = defn.name
        self.schema = defn.args_schema.model_json_schema()

    async def __call__(self, call: ToolCall) -> Any:
        tool_call_id = call.tool_call_id or f"tc_{uuid4()}"

        # BEFORE — tool.called (Engine I6).
        await self._emit(
            type_=EventType.TOOL_CALLED,
            data=ToolCalledData(
                tool_name=call.name,
                tool_call_id=tool_call_id,
                arguments=dict(call.arguments),
            ).model_dump(mode="json"),
            actor=Actor(kind="agent", id=self._agent_id),
        )

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
                ),
            )
        except ToolUnauthorized as exc:
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
            # Diagnostic — return error payload so framework continues.
            return {"error": err.model_dump(mode="json")}
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

        # AFTER — tool.result (Engine I6).
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


def _normalize_result(value: object) -> dict[str, Any]:
    """Wrap a tool result for the event-data dict shape.

    The event payload `tool.result.data.result` is a dict per Schemas
    §B.8. Dict results pass through; everything else is wrapped under
    a `value` key so the verbatim content remains capturable.
    """
    if isinstance(value, dict):
        return value
    return {"value": value}
