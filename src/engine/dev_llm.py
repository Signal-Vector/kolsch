"""Development LLM drivers — :class:`EchoLLMDriver`.

Shipped in ``src/engine/`` (not ``tests/fixtures/``) because the CLI
and API both rely on it as the default-tier LLM. Lets a fresh
install run ``kolsch run <agent>`` end-to-end with zero LLM
keys and zero external services.

Distinct from ``tests/fixtures/fake_llm.FakeLLM`` (which is the
scripted Pydantic-modelled test stub). The dev driver is
runtime-shippable: deterministic, no per-test script, no Pydantic
schema for users to satisfy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engine.framework import LLMResponse

if TYPE_CHECKING:
    from engine.framework import LLMRequest


_DEFAULT_PREFIX = "Echo:"


class EchoLLMDriver:
    """Returns ``"<prefix> <last user message text>"`` as plain text.

    Implements the engine's LLM callable shape — async, accepts
    :class:`LLMRequest`, returns :class:`LLMResponse`. Ships as the
    default-tier LLM so a new user can run ``kolsch run`` against
    a brand-new platform root without configuring a real provider.
    """

    def __init__(self, *, prefix: str = _DEFAULT_PREFIX) -> None:
        self._prefix = prefix

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        last_user = ""
        for msg in reversed(request.messages):
            if msg.role == "user":
                last_user = msg.content
                break
        return LLMResponse(content=f"{self._prefix} {last_user}".strip())
