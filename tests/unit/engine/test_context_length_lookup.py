"""qol-091 — cross-provider ``context_length`` fallback for the engine helper.

Covers the runtime helper ``engine.engine._context_length_for_spec``
and its ``PlatformRuntime.context_length_for`` alias:

- Tier 1 — direct match in the caller's own provider still wins; the
  fallback path is never consulted when the fast path hits.
- Tier 2 — own-provider suffix match (short-form spec vs vendor-prefixed
  catalog and vice-versa).
- Tier 3 — cross-provider fallback with suffix match. Applies to the
  common opencode↔openrouter shape (``kimi-k2.6`` ↔
  ``moonshotai/kimi-k2.6``).
- ``spec.provider == "mock"`` short-circuits to the direct fast path
  only; the mock provider never borrows metadata (its ID space can
  collide with real-provider suffixes ending in ``/mock``).
- Ambiguity across two other providers emits a ``warning`` log naming
  both candidates + values; first-in-config-order wins. Agreement is
  silent. ``None``-valued candidates are skipped entirely (no signal,
  MUST NOT count as ambiguity).
- Helper never raises across degenerate inputs.
- ``PlatformRuntime.context_length_for`` delegates to the same helper
  (parity — both surfaces see the fallback).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pytest

from engine.engine import _context_length_for_spec
from schemas import ModelSpec

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _StubProvider:
    """Minimal ``ProviderConfig`` stand-in — only the ``.models`` attr matters."""

    models: list[Any] = field(default_factory=list)


def _providers(**by_name: list[dict[str, Any]]) -> dict[str, _StubProvider]:
    """Build a providers map preserving insertion order (Python dict guarantee)."""
    return {name: _StubProvider(models=models) for name, models in by_name.items()}


def _spec(provider: str, model: str) -> ModelSpec:
    return ModelSpec(provider=provider, model=model)


# ---------------------------------------------------------------------------
# Test 1 — cross-provider fallback with suffix match
# ---------------------------------------------------------------------------


def test_cross_provider_suffix_match() -> None:
    """opencode entry (no context_length) + openrouter vendor-prefixed hit."""
    providers = _providers(
        opencode=[{"id": "kimi-k2.6"}],
        openrouter=[{"id": "moonshotai/kimi-k2.6", "context_length": 262144}],
    )
    assert _context_length_for_spec(providers, _spec("opencode", "kimi-k2.6")) == 262144


def test_cross_provider_exact_match() -> None:
    """Cross-provider fallback also works on an exact-id match (no suffix)."""
    providers = _providers(
        opencode=[{"id": "kimi-k2.6"}],
        openrouter=[{"id": "kimi-k2.6", "context_length": 200000}],
    )
    assert _context_length_for_spec(providers, _spec("opencode", "kimi-k2.6")) == 200000


# ---------------------------------------------------------------------------
# Test 2 — direct match wins, fallback never consulted
# ---------------------------------------------------------------------------


def test_direct_match_wins() -> None:
    """When the caller's own provider has a direct hit, we return that value.

    The other provider carries a *different* ``context_length`` for the
    suffix-matched form — observing the direct value proves the fallback
    was not consulted.
    """
    providers = _providers(
        opencode=[{"id": "kimi-k2.6", "context_length": 131072}],
        openrouter=[
            {"id": "moonshotai/kimi-k2.6", "context_length": 262144},
        ],
    )
    assert _context_length_for_spec(providers, _spec("opencode", "kimi-k2.6")) == 131072


def test_direct_match_wins_over_own_suffix() -> None:
    """Direct id-equality beats a suffix match in the same provider."""
    providers = _providers(
        opencode=[
            {"id": "moonshotai/kimi-k2.6", "context_length": 100},
            {"id": "kimi-k2.6", "context_length": 200},
        ],
    )
    assert _context_length_for_spec(providers, _spec("opencode", "kimi-k2.6")) == 200


# ---------------------------------------------------------------------------
# Test 3 — no metadata source → None
# ---------------------------------------------------------------------------


def test_no_fallback_source_returns_none() -> None:
    """Only opencode configured, no context_length anywhere → None (unchanged)."""
    providers = _providers(opencode=[{"id": "kimi-k2.6"}])
    assert _context_length_for_spec(providers, _spec("opencode", "kimi-k2.6")) is None


def test_null_context_length_in_other_provider_returns_none() -> None:
    """A None-valued candidate carries no signal — MUST NOT be returned."""
    providers = _providers(
        opencode=[{"id": "kimi-k2.6"}],
        openrouter=[{"id": "moonshotai/kimi-k2.6", "context_length": None}],
    )
    assert _context_length_for_spec(providers, _spec("opencode", "kimi-k2.6")) is None


# ---------------------------------------------------------------------------
# Test 4 — ambiguous cross-provider hit logs warning
# ---------------------------------------------------------------------------


def test_ambiguous_match_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two other providers, different values → first wins + warning fires."""
    providers = _providers(
        opencode=[{"id": "kimi-k2.6"}],
        openrouter=[{"id": "moonshotai/kimi-k2.6", "context_length": 262144}],
        second_source=[{"id": "kimi-k2.6", "context_length": 131072}],
    )
    with caplog.at_level(logging.WARNING, logger="engine"):
        value = _context_length_for_spec(providers, _spec("opencode", "kimi-k2.6"))
    assert value == 262144  # first-in-config-order wins (openrouter)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a warning-level record on ambiguity"
    message = warnings[0].getMessage()
    assert "openrouter" in message
    assert "second_source" in message
    assert "262144" in message
    assert "131072" in message


def test_ambiguous_agreement_is_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two other providers with the SAME value → no ambiguity log."""
    providers = _providers(
        opencode=[{"id": "kimi-k2.6"}],
        openrouter=[{"id": "moonshotai/kimi-k2.6", "context_length": 262144}],
        second_source=[{"id": "kimi-k2.6", "context_length": 262144}],
    )
    with caplog.at_level(logging.WARNING, logger="engine"):
        value = _context_length_for_spec(providers, _spec("opencode", "kimi-k2.6"))
    assert value == 262144
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not warnings, f"unexpected warning(s): {[r.getMessage() for r in warnings]}"


def test_null_candidate_does_not_count_as_ambiguity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A None-valued candidate is skipped entirely — no ambiguity, no warning."""
    providers = _providers(
        opencode=[{"id": "kimi-k2.6"}],
        openrouter=[{"id": "moonshotai/kimi-k2.6", "context_length": 262144}],
        third=[{"id": "kimi-k2.6", "context_length": None}],
    )
    with caplog.at_level(logging.WARNING, logger="engine"):
        value = _context_length_for_spec(providers, _spec("opencode", "kimi-k2.6"))
    assert value == 262144
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not warnings


# ---------------------------------------------------------------------------
# Test — suffix match in both directions
# ---------------------------------------------------------------------------


def test_suffix_match_prefixed_spec_short_candidate() -> None:
    """Reverse direction: spec is vendor-prefixed, candidate is short-form."""
    providers = _providers(
        opencode=[{"id": "moonshotai/kimi-k2.6"}],
        openrouter=[{"id": "kimi-k2.6", "context_length": 262144}],
    )
    assert (
        _context_length_for_spec(providers, _spec("opencode", "moonshotai/kimi-k2.6"))
        == 262144
    )


def test_own_provider_suffix_match() -> None:
    """Same-provider suffix match — short spec vs vendor-prefixed catalog."""
    providers = _providers(
        opencode=[{"id": "moonshotai/kimi-k2.6", "context_length": 262144}],
    )
    assert _context_length_for_spec(providers, _spec("opencode", "kimi-k2.6")) == 262144


# ---------------------------------------------------------------------------
# Test — mock provider short-circuits
# ---------------------------------------------------------------------------


def test_mock_provider_never_borrows_metadata() -> None:
    """spec.provider == 'mock' short-circuits: never enters cross-provider fallback.

    Even when a real provider has an entry whose suffix matches
    ``mock-summariser`` (e.g. ``vendor/mock-summariser``), the helper
    returns ``None``.
    """
    providers = _providers(
        openrouter=[
            {"id": "vendor/mock-summariser", "context_length": 262144},
        ],
    )
    assert _context_length_for_spec(providers, _spec("mock", "mock-summariser")) is None


def test_mock_provider_direct_hit_still_works() -> None:
    """If the mock provider is actually in the config with a value, direct-hit wins."""
    providers = _providers(
        mock=[{"id": "mock-model", "context_length": 8192}],
    )
    assert _context_length_for_spec(providers, _spec("mock", "mock-model")) == 8192


def test_mock_provider_no_own_suffix_lookup() -> None:
    """Even within the mock provider itself, no suffix fallback fires."""
    providers = _providers(
        mock=[{"id": "vendor/mock-model", "context_length": 8192}],
    )
    assert _context_length_for_spec(providers, _spec("mock", "mock-model")) is None


# ---------------------------------------------------------------------------
# Test 5 — helper never raises
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("providers", "provider", "model"),
    [
        (None, "opencode", "kimi-k2.6"),
        ({}, "opencode", "kimi-k2.6"),
        ({"opencode": _StubProvider(models=None)}, "opencode", "kimi-k2.6"),
        ({"opencode": _StubProvider(models=[])}, "opencode", "kimi-k2.6"),
        ({"opencode": _StubProvider(models=["not-a-dict"])}, "opencode", "kimi-k2.6"),
        (
            {
                "opencode": _StubProvider(
                    models=[{"id": "kimi-k2.6", "context_length": True}]
                )
            },
            "opencode",
            "kimi-k2.6",
        ),
        (
            {
                "opencode": _StubProvider(
                    models=[{"id": "kimi-k2.6", "context_length": "big"}]
                )
            },
            "opencode",
            "kimi-k2.6",
        ),
        (
            {"opencode": _StubProvider(models=[{"id": None, "context_length": 100}])},
            "opencode",
            "kimi-k2.6",
        ),
        (
            {"opencode": _StubProvider(models=[{"context_length": 100}])},
            "opencode",
            "kimi-k2.6",
        ),
    ],
)
def test_helper_never_raises(
    providers: dict[str, Any] | None, provider: str, model: str
) -> None:
    """Degenerate shapes must return None, not propagate an exception."""
    assert _context_length_for_spec(providers, _spec(provider, model)) is None


def test_boolean_context_length_returns_none() -> None:
    """``context_length: true`` must not become ``1`` via the bool→int subclass."""
    providers = _providers(
        opencode=[{"id": "kimi-k2.6", "context_length": True}],
    )
    assert _context_length_for_spec(providers, _spec("opencode", "kimi-k2.6")) is None


def test_provider_not_present_falls_through_to_cross() -> None:
    """Spec targets a provider not in the map; cross-provider fallback still fires."""
    providers = _providers(
        openrouter=[{"id": "moonshotai/kimi-k2.6", "context_length": 262144}],
    )
    assert _context_length_for_spec(providers, _spec("opencode", "kimi-k2.6")) == 262144


def test_provider_not_present_and_no_fallback_returns_none() -> None:
    """Spec targets an absent provider and nothing else has a match → None."""
    providers = _providers(
        openrouter=[{"id": "something-else", "context_length": 100}],
    )
    assert _context_length_for_spec(providers, _spec("opencode", "kimi-k2.6")) is None


# ---------------------------------------------------------------------------
# PlatformRuntime parity
# ---------------------------------------------------------------------------


def test_platform_runtime_context_length_for_delegates() -> None:
    """``PlatformRuntime.context_length_for`` must return the same value as the helper.

    Uses a runtime instance built from an empty root — we only exercise
    the helper delegation, not the wider bootstrap.
    """
    from llm.protocol import ProviderConfig
    from platform_runtime.runtime import PlatformRuntime

    providers_cfg = {
        "opencode": ProviderConfig(
            name="opencode",
            type="opencode",
            api_key_secret=None,
            base_url=None,
            timeout_seconds=None,
            models=[{"id": "kimi-k2.6"}],
        ),
        "openrouter": ProviderConfig(
            name="openrouter",
            type="openrouter",
            api_key_secret=None,
            base_url=None,
            timeout_seconds=None,
            models=[
                {"id": "moonshotai/kimi-k2.6", "context_length": 262144},
            ],
        ),
    }

    # Build a bare PlatformRuntime — we only need `llm_providers_config`
    # to route the delegation. All other subsystem fields can stay
    # unset because `context_length_for` never touches them.
    # PlatformRuntime is a frozen dataclass, so bypass __init__ +
    # setattr via object.__setattr__.
    runtime = PlatformRuntime.__new__(PlatformRuntime)
    object.__setattr__(runtime, "llm_providers_config", providers_cfg)

    assert runtime.context_length_for(_spec("opencode", "kimi-k2.6")) == 262144
    # Parity: mirrors the module-level helper.
    assert runtime.context_length_for(_spec("opencode", "kimi-k2.6")) == (
        _context_length_for_spec(providers_cfg, _spec("opencode", "kimi-k2.6"))
    )
