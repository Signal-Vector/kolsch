"""Unit tests for :mod:`cli.chat.state_tokens` (qol-075).

The canonical `STATE_TOKENS` table replaces the two divergent
per-widget glyph maps (`WorkflowNodeRow` / `CardWidget`). Legacy
names alias to canonical via `_LEGACY_ALIASES`; every legacy
resolution emits a `DeprecationWarning`.
"""

from __future__ import annotations

import warnings

import pytest

from cli.chat.state_tokens import (
    _LEGACY_ALIASES,
    STATE_TOKENS,
    StateToken,
    resolve_state,
)


# ---------------------------------------------------------------------------
# Canonical resolution
# ---------------------------------------------------------------------------


_EXPECTED_CANONICAL: dict[str, tuple[str, str, str]] = {
    "success":    ("✓", "$tool-fg", "bold"),
    "failed":     ("✗", "$error",   "bold"),
    "running":    ("▸", "$tool-fg", "bold"),
    "queued":     ("⋯", "$hint",    "dim"),
    "paused":     ("⚠", "$warning", "bold italic"),
    "awaiting":   ("⚠", "$warning", "bold italic"),
    "cancelling": ("⧗", "$warning", "bold italic"),
    "cancelled":  ("─", "$hint",    "dim"),
}


def test_canonical_states_cover_the_full_vocabulary() -> None:
    """`STATE_TOKENS` exposes exactly the 8 canonical names."""
    assert set(STATE_TOKENS) == set(_EXPECTED_CANONICAL), (
        "STATE_TOKENS drifted from the qol-075 canonical vocabulary"
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    list(_EXPECTED_CANONICAL.items()),
    ids=list(_EXPECTED_CANONICAL),
)
def test_canonical_state_resolves_to_expected_token(
    name: str,
    expected: tuple[str, str, str],
) -> None:
    """Every canonical state returns its `(glyph, color, text_style)`."""
    with warnings.catch_warnings():
        # Canonical resolution must NOT warn.
        warnings.simplefilter("error", DeprecationWarning)
        token = resolve_state(name)
    assert isinstance(token, StateToken)
    assert (token.glyph, token.color, token.text_style) == expected


def test_state_token_is_frozen_dataclass() -> None:
    """`StateToken` is immutable — enables safe module-level reuse."""
    token = resolve_state("success")
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError subclass  # noqa: BLE001,PT011
        token.glyph = "?"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Legacy alias resolution
# ---------------------------------------------------------------------------


_EXPECTED_ALIASES: dict[str, str] = {
    "completed":    "success",
    "error":        "failed",
    "initializing": "queued",
    "starting":     "running",
    "pruned":       "cancelled",
}


def test_legacy_alias_table_covers_all_pre_qol075_names() -> None:
    """`_LEGACY_ALIASES` matches the qol-075 bridge exactly."""
    assert _LEGACY_ALIASES == _EXPECTED_ALIASES


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    list(_EXPECTED_ALIASES.items()),
    ids=list(_EXPECTED_ALIASES),
)
def test_legacy_alias_resolves_to_canonical_with_deprecation_warning(
    legacy: str,
    canonical: str,
) -> None:
    """Each legacy name returns its canonical token and emits exactly one warning."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        token = resolve_state(legacy)

    # Filter for the deprecation warnings from our module.
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(dep) == 1, (
        f"expected exactly one DeprecationWarning for legacy state "
        f"{legacy!r}, got {len(dep)}"
    )
    msg = str(dep[0].message)
    assert legacy in msg and canonical in msg
    assert token is STATE_TOKENS[canonical]


# ---------------------------------------------------------------------------
# Unknown-name behavior
# ---------------------------------------------------------------------------


def test_unknown_state_raises_key_error() -> None:
    """Unrecognised names are neither canonical nor aliased — hard fail."""
    with pytest.raises(KeyError, match="unknown state"):
        resolve_state("nonsense")


def test_unknown_state_does_not_emit_deprecation_warning() -> None:
    """Only legacy aliases warn; unknown names raise without warning."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(KeyError):
            resolve_state("nonsense")
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert dep == []
