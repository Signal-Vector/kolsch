"""Production [`Framework`][engine.framework.Framework] adapter for CrewAI.

Wraps real CrewAI (pinned to `crewai==1.14.6`) so the engine can
drive production agentic workflows without changing its own
contract. The engine never imports this module directly; callers
wire it via `AgentEngine(framework=CrewAIFramework(), ...)`.

Design (per
[framework protocol design](../explanation/framework-protocol-design.md)):

- **Fake-LLM injection via BaseLLM subclass** (`_CrewAILLM`). The
  prereq spike validated this as the only mechanism that lets the
  engine see messages verbatim (post-framework-wrap, pre-LLM-invoke)
  without monkey-patching litellm. CrewAI's
  `Agent(llm=BaseLLM-instance)` bypasses `crewai.llm.LLM` and
  litellm entirely — the engine's
  [`LLMHandle`][engine.framework.LLMHandle] sees exactly what would
  have gone to a real provider.
- **Tool dispatch via BaseTool subclass** (`_CrewAITool`). Every
  framework-issued tool call invokes our wrapper, which routes
  through the engine's
  [`ToolHandle`][engine.framework.ToolHandle]. Sandboxing,
  authorization, and secrets stay engine-owned.
- **Concurrency bridge.** `Crew.kickoff_async` runs the sync
  `kickoff` on a worker thread (`asyncio.to_thread`). Inside that
  thread our sync `_run` / `call` methods invoke the engine's async
  handles. The main event loop is captured at `run_exchange` entry
  and used via
  `asyncio.run_coroutine_threadsafe(coro, main_loop).result()` to
  schedule and block. The engine's per-thread lock then serializes
  emits as designed.
- **Retry composition.** CrewAI's internal retries are disabled
  (`Agent(max_retry_limit=0)`); the engine owns retry per
  `RuntimeSpec.max_retries`.
- **History injection.** CrewAI has no first-class "prior turns"
  parameter on `Task`. The assembled history is prepended as
  preamble text on `Task.description`. The captured
  `model.request.data.messages` records what the LLM actually saw —
  CrewAI's wrap is part of that record.
- **Stdout/stderr capture around `kickoff_async`.** CrewAI 1.14.6
  wires a singleton `EventListener` whose `ConsoleFormatter` is
  hardcoded `verbose=True` regardless of `Agent(verbose=False)` /
  `Crew(verbose=False)`. Failure-path warnings (665 stdout bytes,
  2,673 stderr bytes from `logging` ERROR records) plus ~12 KB of
  Rich panel output leak through during a single LLM error. The
  fix wraps `await crew.kickoff_async()` in
  `contextlib.redirect_stdout` + `redirect_stderr` pointed at
  `io.StringIO` and discards the captured bytes — defense in depth
  against the failure-path warnings, the verbose=True panels, and
  any future regression upstream that re-routes formatter output
  through `sys.stdout` / `sys.stderr`. The user-facing failure
  signal comes from `thread.exchange_failed` events rendered by
  the chat renderer; the engine event stream carries the
  authoritative failure UI.

  Caveat: `redirect_stdout` is process-wide for the duration of
  the await, so concurrent coroutines that also write to stdout
  during the exchange will have their output swallowed too. The
  engine runs one exchange at a time per chat thread, so this is
  fine in practice.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import uuid4

# Heavy imports — only present when crewai is installed (test group).
from crewai import Agent, Crew, Task
from crewai.llms.base_llm import BaseLLM
from crewai.tools.base_tool import BaseTool
from pydantic import BaseModel, ConfigDict, Field

from engine.framework import LLMRequest, Message, ToolCall
from errors import ExchangeCancelled, ModelUpstreamError, PlatformError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from engine.framework import LLMHandle, ToolHandle


# Sentinel passed to CrewAI's ``Agent.max_iter`` when the caller
# requests "no cap" (the protocol's ``max_iterations=None``). CrewAI
# 1.14.6 requires an int (``base_agent.py:258`` — plain ``int`` field,
# default 25, no validator), and ``has_reached_max_iterations``
# (``utilities/agent_utils.py:271-281``) is a simple ``>=`` compare, so
# 10**9 is operationally "unbounded" without any overflow risk. Kept
# at module scope so the sentinel is one named constant, not a magic
# literal embedded at the construction site.
_UNBOUNDED_MAX_ITER = 10**9


class CrewAIFramework:
    """Production framework implementing [`Framework`][engine.framework.Framework]."""

    def __init__(self, *, model_name: str = "fake-crewai") -> None:
        self._model_name = model_name

    async def run_exchange(
        self,
        *,
        system_prompt: str,
        user_message: str,
        history: Sequence[Message] = (),
        llm: LLMHandle,
        tools: Sequence[ToolHandle],
        max_iterations: int | None = None,
        cancellation: Callable[[], bool] | None = None,
        cancel_token: Any = None,
    ) -> str:
        """Drive one CrewAI exchange to completion and return the final text.

        Implements [`Framework`][engine.framework.Framework] over the
        wrapped CrewAI library.
        """
        # Capture the main event loop. Inside Crew.kickoff_async,
        # CrewAI runs the sync executor on a worker thread; our
        # BaseLLM.call and BaseTool._run live in that thread and must
        # schedule the engine's async handles BACK onto this loop.
        main_loop = asyncio.get_running_loop()

        adapter_llm = _CrewAILLM(
            model=self._model_name,
            llm_handle=llm,
            main_loop=main_loop,
            cancellation=cancellation,
            # The raw token gives the worker thread access to
            # `is_cancelled()` polling between `future.result()`
            # timeouts so a hanging LLM call observes cancellation
            # within ~50ms.
            cancel_token=cancel_token,
        )

        adapter_tools: list[BaseTool] = [
            _make_crewai_tool(
                handle=handle, main_loop=main_loop, cancel_token=cancel_token
            )
            for handle in tools
        ]

        # Backstory carries the engine-assembled system prompt; role
        # and goal stay generic so the agent is configured by the
        # engine, not by CrewAI defaults.
        # Pydantic-mypy doesn't model CrewAI's Agent fields against
        # our config; these kwargs match the runtime fields visible
        # via Agent.model_fields.
        agent = Agent(  # type: ignore[call-arg]
            role="Assistant",
            goal="Help the user accurately.",
            backstory=system_prompt or "You are a helpful assistant.",
            llm=adapter_llm,
            tools=adapter_tools,
            max_iter=(
                max_iterations if max_iterations is not None else _UNBOUNDED_MAX_ITER
            ),
            # Engine owns retry per RuntimeSpec.max_retries; disable
            # CrewAI's internal retry so the two layers don't compound.
            max_retry_limit=0,
            verbose=False,
            allow_delegation=False,
        )

        task_description = _format_task_description(user_message, history)
        task = Task(
            description=task_description,
            expected_output="A helpful response.",
            agent=agent,
        )

        crew = Crew(
            agents=[agent],
            tasks=[task],
            verbose=False,
            # Inspired by crewAI/crewAI#3789 — tracing=False short-circuits
            # CrewAI's tracing setup + the "Tracing Status" info panel +
            # the first-time-consent prompt path. Our G1 wrap already
            # discards those bytes, so this is belt-and-suspenders: it
            # avoids the wasted CPU of rendering panels that get thrown
            # away, and it neutralises any consent-prompt path that might
            # block on stdin. Confirmed in CrewAI 1.14.6: the tracing
            # parameter is honoured at the top of should_enable_tracing
            # (events/listeners/tracing/utils.py:96).
            tracing=False,
        )

        # asyncio.to_thread inside kickoff_async will pick up
        # main_loop's contextvars but its own asyncio loop is
        # transient; our threadsafe scheduling keeps emits on main_loop.
        #
        # Capture-and-discard wrap — see the module docstring's
        # "Stdout/stderr capture around kickoff_async" section for
        # the why (CrewAI 1.14.6's verbose=True singleton + failure-
        # path warnings being noise, not signal). Do NOT remove
        # without first checking that the upstream singleton has
        # been fixed.
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            try:
                result = await crew.kickoff_async()
            except PlatformError:
                # Engine-typed errors (e.g., ExchangeCancelled raised
                # from inside _CrewAILLM.call) pass through unchanged
                # so the retry loop / boundary classify them as already-
                # typed.
                raise
            except ConnectionError as exc:
                # CrewAI's OpenAI adapter re-raises every
                # `openai.APIConnectionError` (incl. `APITimeoutError`)
                # as a bare `builtins.ConnectionError` with the prefix
                # "Failed to connect to OpenAI API:". Translate to
                # ModelUpstreamError (Recoverable) so the engine's
                # retry pipeline runs — the user's configured
                # `runtime.max_retries` becomes effective for
                # transient transport failures instead of collapsing
                # the whole exchange on the first hiccup.
                #
                # `from exc` preserves the cause chain so the
                # engine's cause-chain inspector can still classify
                # accurately if a future CrewAI release flattens the
                # type.
                raise ModelUpstreamError(
                    provider="crewai",
                    http_status=0,
                    response=str(exc),
                ) from exc
        return _extract_text(result)


# ---------------------------------------------------------------------------
# _CrewAILLM — the BaseLLM subclass (Fake-LLM injection mechanism)
# ---------------------------------------------------------------------------


class _CrewAILLM(BaseLLM):
    """BaseLLM subclass routing every call through the engine's LLMHandle.

    Engine-internal (leading underscore) — the public surface is
    [`CrewAIFramework`][engine.crewai_framework.CrewAIFramework].

    **Why BaseLLM subclass over alternatives:**

    - litellm monkeypatch replaces `litellm.completion` globally,
      which would touch every other test running in the same
      process, and would not capture the
      post-`_format_messages_for_provider` message shape because
      `crewai.llm.LLM` reformats AFTER the monkey point. Rejected.
    - Callback injection (`Agent(llm=str, ...)` + after-call hooks)
      fires AFTER the LLM ran; the response cannot be stubbed
      without going through a real LLM first. Rejected.
    - BaseLLM subclass is public, stable since CrewAI PR #2277
      (0.105.x). A subclass wired via `Agent(llm=BaseLLM-instance)`
      bypasses `crewai.llm.LLM` and litellm entirely. The engine
      sees the exact messages CrewAI built (post-wrap, pre-network)
      and chooses what response to return.
    """

    # Pydantic v2 model config. CrewAI's BaseLLM is a Pydantic
    # BaseModel; we add private (non-validated) attributes via
    # PrivateAttr-style assignment so the engine handle and main loop
    # don't pollute the LLM's field schema.
    model_config: ClassVar[ConfigDict] = ConfigDict(
        arbitrary_types_allowed=True,
    )

    # Pass-through fields the base requires. Defaults make this
    # constructible without the caller filling every BaseLLM field.
    llm_type: str = "engine_fake"
    provider: str = "engine"

    # Engine-owned handles. Stored as model fields with arbitrary
    # types allowed; Pydantic accepts them since model_config above
    # opens that door.
    llm_handle: Any = Field(default=None)
    main_loop: Any = Field(default=None)
    cancellation: Any = Field(default=None)
    # The raw CancelToken so the worker thread can poll
    # `is_cancelled()` between `future.result(timeout=...)` polls.
    # `cancellation` (the callable) is kept for backwards compat
    # with the pre-call poll check.
    cancel_token: Any = Field(default=None)

    def supports_function_calling(self) -> bool:
        # Drive CrewAI's native tool-calling path (see
        # crew_agent_executor._invoke_loop) rather than ReAct text
        # parsing. Native gives us a structured tool-call list back
        # that matches our engine.framework.LLMResponse.tool_calls
        # shape.
        return True

    def call(  # type: ignore[override]
        self,
        messages: str | list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        callbacks: list[Any] | None = None,  # noqa: ARG002
        available_functions: dict[str, Any] | None = None,  # noqa: ARG002
        from_task: Any | None = None,  # noqa: ARG002
        from_agent: Any | None = None,  # noqa: ARG002
        response_model: type[BaseModel] | None = None,  # noqa: ARG002
    ) -> str | Any:
        # Normalize to list[dict].
        msgs: list[dict[str, Any]]
        if isinstance(messages, str):
            msgs = [{"role": "user", "content": messages}]
        else:
            msgs = list(messages)

        # Cancellation poll BEFORE making the network call. Raise the
        # catalog-typed Fatal so the engine's _run_framework path catches
        # it as a PlatformError and routes to thread.exchange_failed
        # with the canonical `exchange.cancelled` code (handbook hard
        # rule — no non-PlatformError escapes a public boundary).
        if self.cancellation is not None and self.cancellation():
            raise ExchangeCancelled(cancelled_at_event_id="")

        request = LLMRequest(
            messages=[_to_engine_message(m) for m in msgs],
            tools=list(tools or []),
            model=self.model,
            parameters={},
        )

        # Schedule the async LLMHandle on the engine's main loop
        # (we're inside CrewAI's worker thread from asyncio.to_thread).
        # The poll-with-cancel loop replaces an indefinite
        # `future.result()` block so a hanging LLM observes cancel
        # within ~50ms of the flip.
        from engine._cancellation import wait_future_with_cancel  # noqa: PLC0415

        coro = self.llm_handle(request)
        future = asyncio.run_coroutine_threadsafe(coro, self.main_loop)
        response = wait_future_with_cancel(
            future, self.cancel_token, what="crewai_llm_call"
        )

        # Translate engine LLMResponse → CrewAI's expected shape.
        # Per crew_agent_executor._is_tool_call_list, an OpenAI-style
        # dict with a `function` key is recognised as a tool call.
        if response.tool_calls:
            return [
                {
                    "id": call.get("id") or f"call_{uuid4().hex[:8]}",
                    "function": {
                        "name": call["name"],
                        "arguments": (
                            json.dumps(call["arguments"])
                            if isinstance(call.get("arguments"), dict)
                            else call.get("arguments", "{}")
                        ),
                    },
                }
                for call in response.tool_calls
            ]
        return response.content or ""


# ---------------------------------------------------------------------------
# _CrewAITool — the BaseTool subclass routing through ToolHandle
# ---------------------------------------------------------------------------


def _make_crewai_tool(
    *,
    handle: ToolHandle,
    main_loop: asyncio.AbstractEventLoop,
    cancel_token: Any = None,
) -> BaseTool:
    """Build a concrete BaseTool subclass bound to one ToolHandle.

    The subclass is created per-handle so each tool surfaces its own
    `name`, `description`, and `args_schema` to CrewAI's tool
    converter. `_run` schedules the engine's async
    [`ToolHandle`][engine.framework.ToolHandle] on the captured main
    loop and blocks for the result — the engine's per-thread emit
    lock then serializes event construction.

    `cancel_token` is the exchange's
    [`CancelToken`][engine.context.CancelToken] (or None for
    pre-cancel paths). When set, `_run` uses
    `wait_future_with_cancel` so a hanging tool observes
    cancellation in the worker thread within ~50ms instead of
    blocking indefinitely on `future.result()`.
    """
    from engine._cancellation import wait_future_with_cancel  # noqa: PLC0415

    name = handle.name
    schema = handle.schema or {}
    description = str(schema.get("description") or f"Tool: {name}")

    # Build a minimal Pydantic schema that accepts whatever the LLM
    # emits as arguments — CrewAI will call _run with **kwargs. The
    # engine's tools subsystem re-validates inside invoke_tool, so
    # this schema only needs to accept dicts.
    args_schema = _build_passthrough_schema(name, schema)

    class _Tool(BaseTool):
        # Class-level attributes CrewAI's BaseTool reads at construction.
        model_config: ClassVar[ConfigDict] = ConfigDict(
            arbitrary_types_allowed=True,
        )

        def _run(self, **kwargs: Any) -> Any:
            coro = handle(ToolCall(name=name, arguments=dict(kwargs)))
            future = asyncio.run_coroutine_threadsafe(coro, main_loop)
            result = wait_future_with_cancel(
                future, cancel_token, what=f"crewai_tool[{name}]"
            )
            # Tool result must be a string for CrewAI's tool-message
            # synthesis; JSON-serialize dicts deterministically.
            if isinstance(result, str):
                return result
            return json.dumps(
                result,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )

    return _Tool(
        name=name,
        description=description,
        args_schema=args_schema,
    )


_JSON_TYPE_TO_PYTHON: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _pytype_for_property(prop: Any) -> Any:
    """Map a JSON-schema property's `type` to a Python type.

    Handles both scalar `"type": "string"` and array-form `"type":
    ["string", "null"]` (Pydantic → JSON Schema emits the latter for
    `str | None`). Falls back to `Any` for missing / unknown types so
    the field still round-trips.
    """
    if not isinstance(prop, dict):
        return Any
    t = prop.get("type")
    if isinstance(t, str):
        return _JSON_TYPE_TO_PYTHON.get(t, Any)
    if isinstance(t, list):
        for entry in t:
            if isinstance(entry, str) and entry != "null":
                return _JSON_TYPE_TO_PYTHON.get(entry, Any)
    return Any


def _build_passthrough_schema(
    tool_name: str,
    schema: dict[str, Any],
) -> type[BaseModel]:
    """Return a Pydantic model that advertises the tool's real parameters.

    The model rebuilds each property from the source JSON Schema with
    its real `type` and `description` so CrewAI's
    `convert_tools_to_openai_schema` produces a fully-typed OpenAI
    tool spec. Losing the type here was the root cause of the
    Kimi K2 "empty arguments" bug: models rely on the type + description
    to decide what to send; a schema with only field names and
    `default: null` gives them no signal. Smarter models (Claude,
    GPT-4) infer from the tool name and user prompt anyway;
    Kimi doesn't, and returns `arguments: {}` verbatim.

    Actual argument validation still runs downstream inside
    `ToolsSubsystem.invoke_tool` against the tool's real
    `args_schema` — this model is used only to advertise to the LLM.
    Fields absent from `schema.required` accept `None` so the LLM can
    omit them; the tool subsystem re-validates against real
    constraints.
    """
    from pydantic import Field, create_model  # noqa: PLC0415

    fields: dict[str, Any] = {}
    properties = schema.get("properties", {})
    required = set(schema.get("required", []) or [])
    if isinstance(properties, dict):
        for key, prop in properties.items():
            pytype = _pytype_for_property(prop)
            description = (
                str(prop.get("description", "")) if isinstance(prop, dict) else ""
            )
            if key in required:
                field_info = Field(..., description=description)
                fields[str(key)] = (pytype, field_info)
            else:
                default = prop.get("default") if isinstance(prop, dict) else None
                field_info = Field(default=default, description=description)
                fields[str(key)] = (pytype | None, field_info)
    if not fields:
        fields["_passthrough"] = (Any, None)
    model: type[BaseModel] = create_model(
        f"_PassthroughArgs_{tool_name}",
        __base__=BaseModel,
        **fields,
    )
    return model


# ---------------------------------------------------------------------------
# Helpers — message translation, task formatting, result extraction
# ---------------------------------------------------------------------------


def _to_engine_message(raw: dict[str, Any]) -> Message:
    """Translate a CrewAI message dict into the engine's `Message`.

    CrewAI uses LLMMessage TypedDict with optional `tool_call_id`,
    `name`, `tool_calls` keys.
    [`Message`][engine.framework.Message] has the same shape but
    with strict typing; unknown keys are dropped.
    """
    content = raw.get("content")
    if content is None:
        content = ""
    elif not isinstance(content, str):
        # Multimodal content blocks — JSON-flatten so the verbatim
        # capture remains string-comparable for golden tests.
        content = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return Message(
        role=str(raw.get("role", "user")),
        content=content,
        tool_call_id=raw.get("tool_call_id"),
        tool_calls=raw.get("tool_calls"),
        name=raw.get("name"),
    )


def _format_task_description(user_message: str, history: Sequence[Message]) -> str:
    """Inline the history into the Task description.

    CrewAI has no first-class "prior turns" parameter; prepending as
    preamble keeps the engine's history visible to the LLM via
    CrewAI's prompt builder. Materialization captures whatever
    CrewAI ultimately sends to the LLM, so replay sees the wrapped
    view (not the pre-wrap engine view).
    """
    if not history:
        return user_message
    lines: list[str] = ["Previous conversation:"]
    for msg in history:
        prefix = msg.role.capitalize()
        lines.append(f"{prefix}: {msg.content}")
    lines.append("")
    lines.append(f"Current message: {user_message}")
    return "\n".join(lines)


def _extract_text(result: Any) -> str:
    """Pull the final text from a `CrewOutput`.

    `CrewOutput.raw` is the canonical attribute in v1.14.x; falls
    back to `str(result)` defensively in case CrewAI shifts the
    shape in a future bump.
    """
    raw = getattr(result, "raw", None)
    if isinstance(raw, str):
        return raw
    return str(result)
