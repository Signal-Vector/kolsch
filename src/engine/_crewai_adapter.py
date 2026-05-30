"""crewai.LLM → LLMHandle adapter — completes Phase 1.1 deferred work.

Phase 1.1 (Step 16) shipped :func:`llm.build_llm` and the per-connector
``build_llm`` methods, but the *engine* takes a callable of shape
``(LLMRequest) -> Awaitable[LLMResponse]`` and ``crewai.LLM`` exposes
a synchronous ``.call(messages: list[dict])`` returning ``str``. The
adapter that bridges those two shapes was explicitly deferred in
Step 16's commit message ("crewai.LLM → LLMHandle adapter is not
yet wired").

This module is that adapter. Two responsibilities:

- :func:`make_llm_callable` — wrap a single ``crewai.LLM`` instance
  into an ``LLMHandle``-shaped async callable. Used by the runtime's
  per-spec factory.
- :func:`make_build_llm_factory` — return a per-spec factory that
  calls :func:`llm.build_llm` to construct the ``crewai.LLM`` lazily
  and threads :class:`engine.dev_llm.EchoLLMDriver` for
  ``provider: "fake"`` so the no-creds tutorial path (acceptance
  criterion #3 of the bug ticket) keeps working.

Neither function performs I/O at module-import time; ``crewai.LLM``
construction happens inside the factory (i.e., per exchange when
the engine's pre-flight step 6 first requests a per-attempt client).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

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
    """Wrap a ``crewai.LLM`` in an async callable matching the engine's
    LLMHandle shape.

    Translation:

    - ``LLMRequest.messages`` (list of :class:`engine.framework.Message`)
      → list of OpenAI-shape ``{role, content}`` dicts that
      ``crewai.LLM.call`` accepts.
    - ``crewai.LLM.call`` is synchronous; we wrap in
      ``asyncio.to_thread`` so it doesn't block the engine's event
      loop while the provider responds.
    - ``crewai.LLM.call`` returns a ``str`` (the assistant's reply);
      wrap in :class:`LLMResponse` with ``content=text``.

    Tool calling / function calling are deferred — the wrapper sends
    the request's ``tools`` list through but does not yet parse tool
    invocations out of the returned text. Phase 1 agents that
    actually use tools take the FakeFramework path; CrewAIFramework
    integration with tool dispatch is its own follow-up patch.
    """

    async def call(request: LLMRequest) -> LLMResponse:
        # crewai's LLMMessage TypedDict expects role+content; our
        # `Message.content` is `str` (never None at this seam), and
        # the engine emits `system`/`user`/`assistant`/`tool` roles
        # which match LLMMessage's Literal exactly. cast is safe.
        messages = [{"role": m.role, "content": m.content} for m in request.messages]
        text = await asyncio.to_thread(crewai_llm.call, messages)  # type: ignore[arg-type]
        if not isinstance(text, str):
            # crewai.LLM.call returns `str | Any`; for non-text
            # responses (structured output, tool calls) the engine
            # currently expects content text. Stringify so the
            # exchange still completes; richer parsing is the
            # follow-up patch.
            text = str(text)
        return LLMResponse(content=text)

    return call


def make_build_llm_factory(
    *,
    llm_providers_config: dict[str, ProviderConfig],
    secrets: SecretsProvider,
) -> Callable[[ModelSpec], Awaitable[Callable[[LLMRequest], Awaitable[LLMResponse]]]]:
    """Return the per-spec LLM factory the engine wires via
    ``llm_for_model=``.

    Behaviour:

    - ``spec.provider == "fake"`` → return the
      :class:`engine.dev_llm.EchoLLMDriver` callable. This preserves
      the no-creds tutorial path (bug ticket AC #3) even when real
      providers ARE configured.
    - Anything else → call :func:`llm.build_llm` to get a
      ``crewai.LLM`` for the spec, then wrap via
      :func:`make_llm_callable`. ``build_llm`` raises
      :class:`errors.LlmProviderNotConfigured` (Fatal) if the
      spec's provider isn't in ``llm_providers_config`` — the
      engine surfaces that as ``thread.exchange_failed`` with a
      clear message rather than the silent-echo bug this patch
      closes.

    The factory is async per the engine's LLMFactory contract.
    Imports of ``llm.build_llm`` and ``dev_llm`` are lazy so the
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
