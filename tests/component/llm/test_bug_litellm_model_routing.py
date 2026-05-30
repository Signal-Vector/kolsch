"""Regression test for bug-litellm-model-routing.md.

Pre-fix: ``OpenCodeConnector.build_llm("qwen3.6-plus", …)`` returned
a ``crewai.LLM`` whose ``model`` field was ``"openai/qwen3.6-plus"``.
CrewAI 1.14.6's native ``openai/`` provider validates the model_part
against OpenAI's canonical list; for non-OpenAI names it falls back
to LiteLLM, which is not installed (no ``crewai[litellm]`` extra in
``pyproject.toml``), so the user got the
``ImportError("Unable to initialize LLM with model 'openai/...'. The
model did not match any supported native provider…")`` error.

Post-fix: the connector emits ``"hosted_vllm/<model_id>"`` —
CrewAI's idiom for "OpenAI-compatible endpoint serving arbitrary
models." ``hosted_vllm`` accepts any string as its model_part
(``crewai/llm.py:483-485`` — "vLLM serves any model") and routes
through the same ``OpenAICompatibleCompletion`` client the
``openai/`` route would have used, with identical
``base_url`` / ``api_key`` / ``timeout`` kwargs.

This test FAILS on pre-fix code (the ``openai/`` route) and PASSES
on post-fix code (the ``hosted_vllm/`` route). It does NOT touch
network — it just asserts on the constructed ``crewai.LLM``
attributes.
"""

from __future__ import annotations

import pytest

from llm import ProviderConfig
from llm.providers.opencode import OpenCodeConnector


@pytest.mark.parametrize(
    "model_id",
    [
        # The exact ID from the user's bug report.
        "qwen3.6-plus",
        # Other non-OpenAI-canonical IDs OpenCode commonly serves —
        # all of which would have failed the pre-fix `openai/` route.
        "gpt-oss-120b",
        "qwen2.5-coder-32b",
        "deepseek-coder-v2",
    ],
)
async def test_tc_bug_litellm_routing_uses_hosted_vllm_prefix(
    model_id: str,
) -> None:
    """The connector emits ``hosted_vllm/<model_id>`` exactly — the
    one route in CrewAI 1.14.6 that pairs a permissive model-name
    validator with the OpenAI-compat client without requiring
    ``litellm`` to be installed."""
    connector = OpenCodeConnector()
    config = ProviderConfig(
        name="opencode",
        type="opencode",
        api_key_secret="OPENCODE_API_KEY",
        base_url="https://opencode.example.com/v1",
        timeout_seconds=30,
        extras={},
        models=[],
    )
    llm = await connector.build_llm(model_id, config, "sk-test")

    # The chosen route (acceptance criterion 5 — assert the model
    # field uses the route that actually works).
    assert llm.provider == "hosted_vllm", (
        f"bug regression: connector emitted provider {llm.provider!r}, "
        f"expected 'hosted_vllm' — see "
        f"docs/process/bug-litellm-model-routing.md"
    )
    assert llm.model == model_id, (
        f"bug regression: model_part is {llm.model!r}, expected "
        f"{model_id!r} unchanged — the connector must not mutate IDs"
    )
    # The base_url and timeout kwargs the pre-fix code passed through
    # are preserved unchanged; the parent OpenAICompletion class
    # consumes them at the same names.
    assert llm.base_url == "https://opencode.example.com/v1"
    assert llm.timeout == 30


async def test_tc_bug_litellm_routing_constructed_llm_is_usable() -> None:
    """End-to-end sanity: the constructed ``crewai.LLM`` is the actual
    OpenAI-compatible CrewAI native class — not the LiteLLM fallback,
    not the raw ``LLM`` wrapper. If a future CrewAI version moves
    ``hosted_vllm`` out of the native set, this test fails fast and
    points the next contributor at the routing-choice docstring."""
    connector = OpenCodeConnector()
    config = ProviderConfig(
        name="opencode",
        type="opencode",
        api_key_secret="OPENCODE_API_KEY",
        base_url="https://opencode.example.com/v1",
        timeout_seconds=None,
        extras={},
        models=[],
    )
    llm = await connector.build_llm("qwen3.6-plus", config, "sk-test")
    # `is_litellm` is the CrewAI flag distinguishing native vs.
    # LiteLLM-fallback construction. Native = no litellm dep needed.
    assert getattr(llm, "is_litellm", False) is False, (
        "bug regression: the constructed LLM took the LiteLLM "
        "fallback path — the routing choice intent was to avoid "
        "depending on the litellm extra. Re-read the connector "
        "module docstring."
    )
