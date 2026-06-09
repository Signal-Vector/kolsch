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
        # Bug-engine-tools-wiring-gap.md Step F — include the
        # tool-schema list the framework forwarded to the LLM.
        # ``request.tools`` is the canonical post-framework-wrap
        # source for what the model actually saw (CrewAI / other
        # frameworks may reshape; the engine emits what crossed
        # the seam, not the engine-assembled pre-wrap list).
        request_data = ModelRequestData(
            call_id=call_id,
            model=dict(self._model_spec),
            config_event_id=EventId(self._config_event_id or "ev_pending"),
            messages=[m.model_dump(mode="json") for m in request.messages],
            tools=list(request.tools),
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
                    tool_call_id=tool_call_id,
                ),
            )
        except ToolUnauthorized as exc:
            err = Error.from_platform_error(exc)
            # qol-permission-system-human-readable.md Step 3 — derive
            # the exact `/perm allow` chat command from the matched
            # rule's `what` + the in-flight args. The model + the
            # chat renderer both see this hint; the user types it
            # to un-block.
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


# ---------------------------------------------------------------------------
# qol-permission-system-human-readable.md Step 3 — denied recovery hint
# ---------------------------------------------------------------------------


_TARGET_ARG_PRIORITY = ("path", "command", "url")
"""Same order as ``tools.dispatch._TARGET_ARG_PRIORITY``. Duplicated
here (rather than imported) because the observer can't take a
dependency on the dispatcher's internal helper — both modules read
the convention from the same spec."""


def _derive_authz_recovery_hint(
    exc: ToolUnauthorized,
    *,
    arguments: dict[str, Any] | None,
) -> str | None:
    """Build the ``/perm allow <tool> <pattern>`` hint string.

    Reads :attr:`ToolUnauthorized.detail`'s ``matched_rule`` to find
    the qualified tool ref (or falls back to the legacy action
    namespace ``tool.<name>`` when the matched rule's ``what`` is
    in old shape). The suggested pattern is derived from the
    in-flight arguments via the same convention the modal uses
    (first whitespace-bounded token for bash; parent dir for file
    tools; ``*`` otherwise). Returns ``None`` if there's nothing
    actionable to surface (e.g., no rule matched — pure default
    deny — in which case the user can use ``/perm allow
    <ref> *``).

    The pattern is intentionally **broad** by default (``*``); the
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
    if isinstance(what, str) and "/" in what:
        tool_ref = what
    else:
        tool_ref = None

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
    """Mirror the modal's :func:`cli._modal.suggest_pattern`.

    Same three heuristics in priority order:

    1. bash with a command target: first whitespace-bounded
       token → ``"<token> *"``.
    2. file tools with a path target: parent-dir glob
       ``"<parent>/*"``.
    3. fallback: ``"*"``.

    Kept independent of the modal so the engine can run without
    importing chat code (Engine I1: engine doesn't depend on
    cli/).
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
