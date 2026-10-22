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
"mock"` so the no-creds tutorial path keeps working.

The callable is also the LLM **call seam** (bug-145): a raw provider
exception from `crewai.LLM.call` is handed to the building connector's
`classify_call_error` hook and re-raised as a typed `model.*`
`PlatformError`, so the fallback loop, compaction and the event log
all see the real failure. The engine never imports a provider SDK.

Neither function performs I/O at module-import time; `crewai.LLM`
construction happens inside the factory — per exchange when the
engine's pre-flight first requests a per-attempt client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from engine.framework import LLMResponse
from errors import ModelTimeout, PlatformError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import crewai

    from engine.framework import LLMCallable, LLMFactory, LLMRequest
    from llm import LLMCallContext, ProviderConfig
    from providers import SecretsProvider
    from schemas import ModelSpec


_log = logging.getLogger("engine.crewai_adapter")


# spec-069 Part A — bound the blocking LLM read so a wedged provider
# call fails fast instead of pinning a worker thread indefinitely.
# `asyncio.wait_for` hard-cancels the awaiting task on expiry; the
# underlying `to_thread` worker keeps running until it returns from
# the blocking call (Python cannot cancel arbitrary threads). That's
# acceptable — spec-069 Part B (owned executor) makes the worker
# daemonic so it can't block interpreter shutdown; until Part B
# lands, the process's non-daemon pool worker keeps the runtime
# alive briefly on shutdown but no longer blocks the CALLER (chat
# session, workflow node) that raised the timeout.
#
# bug-086 — resolution order for the effective timeout:
#   1. `KOLSCH_LLM_CALL_TIMEOUT_SECONDS` env var (float; blank/`0`/
#      negative disables the wrap; malformed falls through).
#   2. Bootstrap-configured default from `platform.yaml`'s
#      `workflows.llm_call_timeout_seconds`, installed via
#      `set_llm_call_timeout_default`.
#   3. Hardcoded `_DEFAULT_LLM_CALL_TIMEOUT_SECONDS` fallback.
#
# The 210s hardcoded fallback = the opencode provider's tuned 180s
# HTTP timeout + 30s margin, so a bare-install (no `platform.yaml`
# knob, no env override) does not undercut the provider — the
# spec-069 regression the bug fixes. When platform.yaml is present
# bootstrap replaces this with `max(provider.timeout_seconds) + 30s`
# across configured providers.


_DEFAULT_LLM_CALL_TIMEOUT_SECONDS: float = 210.0

# Sentinel — a `None` bootstrap-configured value is distinct from
# "bootstrap never ran" (tests, direct engine embedders, tools that
# import `make_llm_callable` outside a runtime). Sentinel = fall
# through to the hardcoded default; explicit `None` = disable.
_UNSET_CONFIGURED_DEFAULT: object = object()
_configured_default_timeout: float | None | object = _UNSET_CONFIGURED_DEFAULT


def set_llm_call_timeout_default(value: float | None) -> None:
    """Install the bootstrap-resolved LLM-call timeout default.

    Called by `bootstrap_runtime` after reading
    `platform.yaml:workflows.llm_call_timeout_seconds`. The value
    becomes the fallback used when `KOLSCH_LLM_CALL_TIMEOUT_SECONDS`
    is unset or malformed. `None` disables the wrap (unbounded call);
    a positive float bounds every subsequent call.

    Process-global by design — the env var is process-global, and
    the platform.yaml value is its persistent sibling. Callers that
    construct their own `CrewAIFramework` outside `bootstrap_runtime`
    can invoke this directly to tune the default.
    """
    global _configured_default_timeout  # noqa: PLW0603
    _configured_default_timeout = value


def _read_llm_call_timeout_seconds() -> float | None:
    raw = os.environ.get("KOLSCH_LLM_CALL_TIMEOUT_SECONDS")
    if raw is not None:
        stripped = raw.strip()
        if not stripped:
            return None
        try:
            value = float(stripped)
        except ValueError:
            # Malformed env — fall through to the bootstrap-configured
            # default (defensive: better a bounded call than an
            # unbounded one when the operator typo'd the env var).
            return _resolve_configured_default()
        if value <= 0:
            return None
        return value
    return _resolve_configured_default()


def _resolve_configured_default() -> float | None:
    if _configured_default_timeout is _UNSET_CONFIGURED_DEFAULT:
        return _DEFAULT_LLM_CALL_TIMEOUT_SECONDS
    # After the sentinel check, the remaining type is `float | None`.
    return _configured_default_timeout  # type: ignore[return-value]


def make_llm_callable(
    crewai_llm: crewai.LLM,
    *,
    classify: Callable[[BaseException], PlatformError | None] | None = None,
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

    Error classification (bug-145): `classify` is the building
    connector's bound `classify_call_error` hook (see
    [`llm.call_error_classifier`][llm.build_llm.call_error_classifier]).
    When `crewai_llm.call` raises an `Exception` that is not a
    `PlatformError`, the callable raises `classify(exc)` `from exc`.
    If `classify` is `None`, returns `None`, or itself raises (logged),
    the original exception is re-raised unchanged. `PlatformError`s
    (including the adapter's own timeout `ModelTimeout`) and
    `asyncio.CancelledError` pass through untouched.
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
        # ``tools: []`` per the OpenAI-compat spec. Also normalise to
        # OpenAI spec shape — the engine's `{name, schema}` internal
        # shape (as built by MinimalFramework) is opaque to litellm;
        # Kimi K2 in particular emits `arguments: {}` for every tool
        # call when it can't find `function.parameters`.
        tools_arg = (
            [_tool_to_openai_spec(t) for t in request.tools] if request.tools else None
        )
        # bug-145 call seam: a raw provider exception becomes the
        # connector's typed `model.*` error before FallbackLLMHandle
        # sees it. PlatformErrors (incl. the timeout below) pass through.
        try:
            result = await _invoke(messages, tools_arg)
        except PlatformError:
            raise
        except Exception as exc:
            classified = _classify_call_error(classify, exc)
            if classified is None:
                raise
            raise classified from exc
        return _engine_response_from_crewai(result)

    async def _invoke(messages: list[dict[str, Any]], tools_arg: Any) -> Any:
        # crewai's LLMMessage is a TypedDict variant we don't import
        # to avoid the heavy crewai imports at module load; our dict
        # has the same shape. Cast is safe.
        #
        # spec-069 Part A — bound the `to_thread` await with
        # `asyncio.wait_for` so a wedged provider read fails fast
        # instead of pinning the worker forever. See the module-level
        # `_read_llm_call_timeout_seconds` docstring for the caveat
        # about the underlying thread outliving the timeout (fixed by
        # Part B / owned executor). `None` (env opt-out) disables the
        # wrap, restoring the pre-spec-069 unbounded shape for tests
        # that need it.
        timeout = _read_llm_call_timeout_seconds()
        if timeout is None:
            return await asyncio.to_thread(
                crewai_llm.call,
                messages,  # type: ignore[arg-type]
                tools=tools_arg,
            )
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    crewai_llm.call,
                    messages,  # type: ignore[arg-type]
                    tools=tools_arg,
                ),
                timeout=timeout,
            )
        except TimeoutError as exc:
            # Semantic domain error rather than a bare `TimeoutError`
            # so the engine's fallback ladder + observer render
            # the same shape as a provider-side `model.timeout`.
            # Provider + model are best-effort — this call site
            # is inside `make_llm_callable` which was constructed
            # for a specific `crewai_llm` with `crewai_llm.model`
            # set.
            model_name = getattr(crewai_llm, "model", None) or "unknown"
            raise ModelTimeout(
                provider="crewai",
                model=str(model_name),
                timeout_seconds=float(timeout),
                where="engine._crewai_adapter.make_llm_callable",
            ) from exc

    return _quiet_crewai_console(call)


def _quiet_crewai_console(
    call: Callable[[LLMRequest], Awaitable[LLMResponse]],
) -> Callable[[LLMRequest], Awaitable[LLMResponse]]:
    """Silence CrewAI's console output for the duration of each `call` (bug-148).

    CrewAI's event listener prints rich panels (`❌ LLM Error`) and
    `[CrewAIEventsBus]` warnings to stdout on every failed call, which
    corrupts `kolsch run`'s JSON stdout and duplicates what the event
    log already records (`thread.exchange_failed`). The engine already
    treats CrewAI console output as noise (`crewai_framework`'s
    `redirect_stdout` capture); this is the same policy for the direct
    `crewai.LLM.call` seam every surface shares (run, chat, workflows,
    compaction, the API server).

    Uses CrewAI's own `set_suppress_console_output` ContextVar, which
    both `ConsoleFormatter.print` and `Printer.print` honour. It is set
    in the awaiting task's context for exactly the span of the call and
    restored in `finally`: `asyncio.to_thread` copies that context into
    the worker running `crewai_llm.call`, and CrewAI's event bus copies
    the worker's context into its handler executor, so the panels and
    warnings of THIS call are silenced and nothing outside it is.
    Without CrewAI installed this is the identity.
    """
    try:
        from crewai.events.utils.console_formatter import (  # noqa: PLC0415
            set_suppress_console_output,
            should_suppress_console_output,
        )
    except ImportError:
        return call

    async def quiet_call(request: LLMRequest) -> LLMResponse:
        previous = should_suppress_console_output()
        set_suppress_console_output(True)
        try:
            return await call(request)
        finally:
            set_suppress_console_output(previous)

    return quiet_call


def _classify_call_error(
    classify: Callable[[BaseException], PlatformError | None] | None,
    exc: Exception,
) -> PlatformError | None:
    """Run the connector's classifier; never let it mask `exc`.

    A classifier that raises is a connector bug: log it and return
    `None` so the caller re-raises the original provider exception.
    """
    if classify is None:
        return None
    try:
        return classify(exc)
    except Exception:  # noqa: BLE001 — a classifier bug must not mask the provider error
        _log.warning(
            "classify_call_error raised while classifying %s; re-raising the original",
            type(exc).__name__,
            exc_info=True,
        )
        return None


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


def _tool_to_openai_spec(tool: dict[str, Any]) -> dict[str, Any]:
    """Normalise a tool advertisement to OpenAI spec shape.

    The engine's internal shape is
    ``{"name": ..., "description": ..., "schema": {JSON Schema}}`` —
    what ``MinimalFramework.run_exchange`` builds from ``ToolHandle`` and
    what the observer records on ``model.request.tools``. LiteLLM /
    OpenAI-compat providers require the ``{type: "function", function:
    {name, description, parameters}}`` envelope; without the
    ``function.parameters`` key the arg schema doesn't reach the model.

    Kimi K2 surfaces this first: its response drops
    ``function.arguments`` when the tool advertisement lacks proper
    ``parameters``, so every tool call arrives with ``arguments: {}``.
    Smarter models (Claude, GPT-4) infer argument names from the tool
    name plus the user prompt and mask the bug.

    Already-spec-shaped entries (``function`` key present) pass
    through unchanged so the ``_CrewAILLM.call`` path — which receives
    tools already in OpenAI shape from CrewAI's
    ``convert_tools_to_openai_schema`` — is unaffected.
    """
    if "function" in tool:
        out = dict(tool)
        out.setdefault("type", "function")
        return out
    name = str(tool.get("name") or "")
    description = str(tool.get("description") or f"Tool: {name}")
    parameters = tool.get("schema") or {}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


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
) -> LLMFactory:
    """Return the per-spec LLM factory the engine wires via `llm_for_model=`.

    Behaviour:

    - `spec.provider == "mock"` → return the
      [`EchoLLMDriver`][engine.dev_llm.EchoLLMDriver] callable.
      Preserves the no-creds tutorial path even when real providers
      ARE configured.
    - Anything else → call `llm.build_llm` to get a `crewai.LLM` for
      the spec, then wrap via `make_llm_callable` with the spec's
      connector classifier bound
      ([`llm.call_error_classifier`][llm.build_llm.call_error_classifier],
      bug-145) so provider call failures reach the fallback loop as
      typed `model.*` errors. `build_llm`
      raises [`LlmProviderNotConfigured`][errors.LlmProviderNotConfigured]
      (Fatal) if the spec's provider isn't in
      `llm_providers_config` — the engine surfaces that as
      `thread.exchange_failed` with a clear message rather than the
      silent-echo bug. The factory's keyword-only `context` (bug-144)
      is forwarded verbatim to `build_llm`; the mock branch ignores it.

    The factory is async per the engine's LLMFactory contract.
    Imports of `llm.build_llm` and `dev_llm` are lazy so the
    runtime's import graph stays narrow for tests that don't touch
    LLM machinery.
    """

    async def factory(
        spec: ModelSpec,
        /,
        *,
        context: LLMCallContext | None,
    ) -> LLMCallable:
        if spec.provider == "mock":
            from engine.dev_llm import EchoLLMDriver  # noqa: PLC0415

            return EchoLLMDriver().__call__
        from llm import build_llm, call_error_classifier  # noqa: PLC0415

        provider_config = llm_providers_config.get(spec.provider)
        crewai_llm = await build_llm(
            spec,
            provider_config,
            secrets,
            configured_providers=sorted(llm_providers_config),
            context=context,
        )
        # `build_llm` raised `LlmProviderNotConfigured` for a `None` config.
        assert provider_config is not None  # noqa: S101 — narrowed by build_llm
        return make_llm_callable(
            crewai_llm, classify=call_error_classifier(spec, provider_config)
        )

    return factory
