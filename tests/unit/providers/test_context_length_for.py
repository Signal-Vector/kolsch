"""Unit tests for `PlatformRuntime.context_length_for` — feat-054 Phase 1.

The helper walks `self.llm_providers_config` for the (provider, model)
pair and returns the declared `context_length`, or `None` on any miss.
Downstream (feat-054 Phase 2) the compaction pre-flight will use
`None` as the signal to skip compaction and emit a diagnostic.

The method uses only `self.llm_providers_config`, so tests exercise it
via a lightweight stand-in rather than a fully wired
`PlatformRuntime`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from llm.protocol import ProviderConfig
from platform_runtime.runtime import PlatformRuntime
from schemas.config import ModelSpec


def _bind(config: dict[str, ProviderConfig]) -> Any:
    """Return a stand-in `self` that carries only `llm_providers_config`."""
    return SimpleNamespace(llm_providers_config=config)


def _provider(name: str, models: list[dict[str, Any]]) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        type=name,
        api_key_secret=None,
        base_url=None,
        timeout_seconds=None,
        extras={},
        models=models,
    )


def test_returns_declared_context_length_for_matching_model() -> None:
    cfg = {
        "anthropic": _provider(
            "anthropic",
            [
                {"id": "claude-opus-4-7", "context_length": 200_000},
                {"id": "claude-haiku-4-5", "context_length": 100_000},
            ],
        ),
    }
    spec = ModelSpec(provider="anthropic", model="claude-opus-4-7")
    result = PlatformRuntime.context_length_for(_bind(cfg), spec)
    assert result == 200_000


def test_returns_none_when_provider_missing() -> None:
    cfg: dict[str, ProviderConfig] = {}
    spec = ModelSpec(provider="anthropic", model="claude-opus-4-7")
    assert PlatformRuntime.context_length_for(_bind(cfg), spec) is None


def test_returns_none_when_model_missing_from_provider() -> None:
    cfg = {
        "anthropic": _provider(
            "anthropic",
            [{"id": "claude-opus-4-7", "context_length": 200_000}],
        ),
    }
    spec = ModelSpec(provider="anthropic", model="claude-not-in-catalog")
    assert PlatformRuntime.context_length_for(_bind(cfg), spec) is None


def test_returns_none_when_context_length_is_null() -> None:
    # Many opencode models legitimately don't declare a context_length.
    cfg = {
        "opencode": _provider(
            "opencode",
            [{"id": "some-model", "context_length": None}],
        ),
    }
    spec = ModelSpec(provider="opencode", model="some-model")
    assert PlatformRuntime.context_length_for(_bind(cfg), spec) is None


def test_returns_none_when_context_length_field_absent() -> None:
    cfg = {
        "opencode": _provider("opencode", [{"id": "some-model"}]),
    }
    spec = ModelSpec(provider="opencode", model="some-model")
    assert PlatformRuntime.context_length_for(_bind(cfg), spec) is None


def test_returns_none_when_context_length_is_bool() -> None:
    # `bool` is a subclass of `int` in Python — the helper must reject
    # `context_length: true` before it gets mistaken for `1`.
    cfg = {
        "quirky": _provider(
            "quirky",
            [{"id": "model-a", "context_length": True}],
        ),
    }
    spec = ModelSpec(provider="quirky", model="model-a")
    assert PlatformRuntime.context_length_for(_bind(cfg), spec) is None


def test_returns_first_matching_entry_when_id_repeats() -> None:
    # Iteration order matches provider config order. First hit wins.
    cfg = {
        "p": _provider(
            "p",
            [
                {"id": "m", "context_length": 1000},
                {"id": "m", "context_length": 2000},
            ],
        ),
    }
    spec = ModelSpec(provider="p", model="m")
    assert PlatformRuntime.context_length_for(_bind(cfg), spec) == 1000
