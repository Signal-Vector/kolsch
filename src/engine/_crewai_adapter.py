"""Bridge `crewai.LLM` (sync) to the engine's `LLMHandle` (async).

The engine takes a callable of shape
`(LLMRequest) -> Awaitable[LLMResponse]` and `crewai.LLM` exposes a
synchronous `.call(messages: list[dict])` returning `str`. This
module provides the two adapters that close that gap:
[`make_llm_callable`][engine._crewai_adapter.make_llm_callable] wraps
a single `crewai.LLM` into an `LLMHandle`-shaped async callable, and
[`make_build_llm_factory`][engine._crewai_adapter.make_build_llm_factory]
returns a per-spec factory that constructs the `crewai.LLM` lazily.
The factory threads
[`EchoLLMDriver`][engine.dev_llm.EchoLLMDriver] for `provider:
"fake"` so the no-creds tutorial path keeps working.

Neither function performs I/O at module-import time; `crewai.LLM`
construction happens inside the factory — per exchange when the
engine's pre-flight first requests a per-attempt client.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from engine.framework import LLMResponse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import crewai

    from engine.framework import LLMRequest
    from llm import ProviderConfig
    from providers import SecretsProvider
    from schemas import ModelSpec


def make_llm_callable(
    crewai_llm: crewai.LLM,
) -> Callable[[LLMRequest], Awaitable[LLMResponse]]:
    """Wrap a `crewai.LLM` in an async callable matching the engine's shape.

    Translation:

    - [`LLMRequest.messages`][engine.framework.LLMRequest] (list of
      [`Message`][engine.framework.Message]) → list of OpenAI-shape
      `{role, content}` dicts that `crewai.LLM.call` accepts.
    - `crewai.LLM.call` is synchronous; wrapped in
      `asyncio.to_thread` so it doesn't block the engine's event
      loop while the provider responds.
    - `crewai.LLM.call` returns a `str` (the assistant's reply);
      wrapped in [`LLMResponse`][engine.framework.LLMResponse] with
      `content=text`.

    Tool calling: the wrapper forwards `request.tools` to
    `crewai_llm.call(tools=...)` so litellm builds the upstream API
    request with the tools array, and parses the response — when the
    model emits a tool_use, `crewai.LLM.call` returns a
    `list[{"id", "function": {"name", "arguments"}}]` (OpenAI
    tool-call shape per litellm); the wrapper translates back into
    `LLMResponse.tool_calls`.
    """

    async def call(request: LLMRequest) -> LLMResponse:
        # crewai's LLMMessage TypedDict expects role+content+optional
        # tool_call_id+tool_calls+name. We must preserve the tool-call
        # bookkeeping fields on the second-and-subsequent turns of a
        # multi-turn tool-using exchange: the assistant message that
        # issued the tool_call carries ``tool_calls``; the tool
        # message that carries the result MUST include
        # ``tool_call_id`` correlating it to that call. Providers
        # (DeepSeek surfaced this first) reject a ``tool`` role
        # message without tool_call_id per the OpenAI-compat spec.
        # Dropping these fields breaks any multi-turn tool exchange.
        messages = [_engine_message_to_crewai(m) for m in request.messages]
        # Forward tools to crewai.LLM.call so litellm includes them
        # in the upstream API request. Pass None (not an empty
        # list) when there are no tools — some providers reject
        # ``tools: []`` per the OpenAI-compat spec.
        tools_arg = list(request.tools) if request.tools else None
        # crewai's LLMMessage is a TypedDict variant we don't import
        # to avoid the heavy crewai imports at module load; our dict
        # has the same shape. Cast is safe.
        result = await asyncio.to_thread(
            crewai_llm.call,
            messages,  # type: ignore[arg-type]
            tools=tools_arg,
        )
        return _engine_response_from_crewai(result)

    return call


def _engine_message_to_crewai(message: Any) -> dict[str, Any]:
    """Render an engine `Message` as the OpenAI-shape dict litellm wants.

    Emits the minimum fields each role needs:

    - `system` / `user` — `{role, content}`.
    - `assistant` — `{role, content}` plus `tool_calls` when the
      assistant emitted any (the previous turn's response).
      `content` may be empty string for assistant messages that ONLY
      emitted tool_calls; the spec accepts `""`.
    - `tool` — `{role, tool_call_id, content}` (and optional `name`).
      The `tool_call_id` MUST match the id from the assistant's
      prior `tool_calls` entry; DeepSeek and the OpenAI reference
      both 400 when it's omitted.

    Optional fields are included only when populated, so a plain
    user message stays a clean `{role, content}` dict and doesn't
    drag along `"tool_call_id": null` etc. that some providers
    reject.
    """
    base: dict[str, Any] = {"role": message.role, "content": message.content}
    tool_call_id = getattr(message, "tool_call_id", None)
    tool_calls = getattr(message, "tool_calls", None)
    name = getattr(message, "name", None)
    if tool_call_id:
        base["tool_call_id"] = tool_call_id
    if tool_calls:
        base["tool_calls"] = [_engine_tool_call_to_openai(tc) for tc in tool_calls]
    if name:
        base["name"] = name
    return base


def _engine_tool_call_to_openai(tc: Any) -> dict[str, Any]:
    """Reshape an engine tool_call entry to OpenAI spec for upstream emission.

    Our engine carries tool_calls in a flat shape
    `{id, name, arguments: dict}` (set by `_normalize_tool_call`
    after parsing the upstream response). The OpenAI-compat spec for
    emitting assistant messages with tool_calls requires
    `{id, type: "function", function: {name, arguments(JSON-string)}}`.

    DeepSeek surfaced the missing-`type` failure first; strict
    OpenAI-compat clients (Together, Anyscale, vLLM/SGLang
    deployments) also reject the flat shape. The reshape happens at
    the adapter boundary so the engine's internal representation
    stays flat (easier for renderers and replay) and only the
    upstream call sees the spec shape.

    Accepts either flat or already-spec shape — if a tool_call
    already has a `function` key, pass it through verbatim
    (defensive for future code paths that already conform).
    """
    if isinstance(tc, dict) and "function" in tc:
        # Already in spec shape — ensure ``type`` is present.
        out: dict[str, Any] = dict(tc)
        out.setdefault("type", "function")
        return out
    name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
    arguments = (
        tc.get("arguments") if isinstance(tc, dict) else getattr(tc, "arguments", None)
    )
    call_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
    args_str = (
        json.dumps(arguments)
        if isinstance(arguments, dict)
        else (arguments if isinstance(arguments, str) else "{}")
    )
    return {
        "id": call_id or f"call_{uuid4().hex[:8]}",
        "type": "function",
        "function": {
            "name": name or "",
            "arguments": args_str,
        },
    }


def _engine_response_from_crewai(result: Any) -> LLMResponse:
    """Translate `crewai.LLM.call`'s polymorphic return into LLMResponse.

    Three shapes:

    - `str` — assistant text reply → `LLMResponse(content=text)`.
    - `list[dict]` with each dict carrying a `function` key —
      OpenAI tool-call shape that litellm passes through from the
      provider → `LLMResponse(content=None, tool_calls=[...])` with
      each entry normalized to the engine's
      `{id, name, arguments: dict}` form.
    - Anything else — stringify to `content` so the exchange still
      completes rather than crashing on an unknown shape.
    """
    if isinstance(result, list):
        tool_calls: list[dict[str, Any]] = []
        for call in result:
            normalized = _normalize_tool_call(call)
            if normalized is not None:
                tool_calls.append(normalized)
        if tool_calls:
            return LLMResponse(content=None, tool_calls=tool_calls)
        # List but no usable tool calls — stringify so the
        # exchange completes; the engine boundary will treat the
        # text as the assistant's reply.
        return LLMResponse(content=str(result))
    if isinstance(result, str):
        return LLMResponse(content=result)
    return LLMResponse(content=str(result))


def _normalize_tool_call(call: Any) -> dict[str, Any] | None:
    """Coerce one `crewai.LLM.call` tool-call entry to the engine shape.

    Two shapes seen in the wild from `crewai.LLM.call`:

    1. **Plain dict** — emitted by our own `_CrewAILLM` wrapper when
       the test path returns tool calls. Shape:
       `{id, function: {name, arguments(str | dict)}}`.
    2. **litellm Pydantic object** — emitted by the real `crewai.LLM`
       when the upstream provider returns `tool_use`. Shape:
       `ChatCompletionMessageFunctionToolCall(id, function=Function(name,
       arguments(JSON-string)), type="function")`. The repr leaks if
       we `str()` the list.

    Returns `None` when the entry lacks a usable `name` so the
    caller can skip it without crashing.
    """
    name = _attr_or_key(_attr_or_key(call, "function"), "name")
    if not isinstance(name, str) or not name:
        return None
    raw_args = _attr_or_key(_attr_or_key(call, "function"), "arguments")
    if isinstance(raw_args, str):
        try:
            arguments: dict[str, Any] = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError):
            arguments = {}
    elif isinstance(raw_args, dict):
        arguments = raw_args
    else:
        arguments = {}
    call_id = _attr_or_key(call, "id")
    return {
        "id": (
            call_id
            if isinstance(call_id, str) and call_id
            else f"call_{uuid4().hex[:8]}"
        ),
        "name": name,
        "arguments": arguments,
    }


def _attr_or_key(obj: Any, key: str) -> Any:
    """Read `obj[key]` if obj is a dict, else `getattr(obj, key)`.

    Duck-types over the dict-vs-Pydantic-object shape difference
    between our own `_CrewAILLM` (dicts) and real `crewai.LLM` via
    litellm (`ChatCompletionMessageFunctionToolCall` objects).
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def make_build_llm_factory(
    *,
    llm_providers_config: dict[str, ProviderConfig],
    secrets: SecretsProvider,
) -> Callable[[ModelSpec], Awaitable[Callable[[LLMRequest], Awaitable[LLMResponse]]]]:
    """Return the per-spec LLM factory the engine wires via `llm_for_model=`.

    Behaviour:

    - `spec.provider == "fake"` → return the
      [`EchoLLMDriver`][engine.dev_llm.EchoLLMDriver] callable.
      Preserves the no-creds tutorial path even when real providers
      ARE configured.
    - Anything else → call `llm.build_llm` to get a `crewai.LLM` for
      the spec, then wrap via `make_llm_callable`. `build_llm`
      raises [`LlmProviderNotConfigured`][errors.LlmProviderNotConfigured]
      (Fatal) if the spec's provider isn't in
      `llm_providers_config` — the engine surfaces that as
      `thread.exchange_failed` with a clear message rather than the
      silent-echo bug.

    The factory is async per the engine's LLMFactory contract.
    Imports of `llm.build_llm` and `dev_llm` are lazy so the
    runtime's import graph stays narrow for tests that don't touch
    LLM machinery.
    """

    async def factory(
        spec: ModelSpec,
    ) -> Callable[[LLMRequest], Awaitable[LLMResponse]]:
        if spec.provider == "fake":
            from engine.dev_llm import EchoLLMDriver  # noqa: PLC0415

            return EchoLLMDriver().__call__
        from llm import build_llm  # noqa: PLC0415

        crewai_llm = await build_llm(
            spec,
            llm_providers_config.get(spec.provider),
            secrets,
            configured_providers=sorted(llm_providers_config),
        )
        return make_llm_callable(crewai_llm)

    return factory
