"""Unit tests for :mod:`cli.chat.state_tokens`.

The canonical `STATE_TOKENS` table replaces the two divergent
per-widget glyph maps (`WorkflowNodeRow` / `CardWidget`) — shipped
in qol-075. qol-084 dissolved the `_LEGACY_ALIASES` bridge that
briefly mapped the historical vocabulary
(``completed``/``error``/``initializing``/``starting``/``pruned``)
to canonical; the widgets' reactive `state` now holds canonical
values directly and the bridge is gone.

Wire-vocab entries (``CardFinalStatus`` from
``src/tools/services/cards.py``) that still use ``"error"`` /
``"initializing"`` are translated at the widget boundary via the
:data:`WIRE_TO_CANONICAL` mapping — the two-entry table below.
"""

from __future__ import annotations

import warnings

import pytest

from cli.chat.state_tokens import (
    STATE_TOKENS,
    WIRE_TO_CANONICAL,
    StateToken,
    resolve_state,
)

# ---------------------------------------------------------------------------
# Canonical resolution
# ---------------------------------------------------------------------------


_EXPECTED_CANONICAL: dict[str, tuple[str, str, str]] = {
    "success": ("✓", "$tool-fg", "bold"),
    "failed": ("✗", "$error", "bold"),
    "running": ("▸", "$tool-fg", "bold"),
    "queued": ("⋯", "$hint", "dim"),
    "paused": ("⚠", "$warning", "bold italic"),
    "awaiting": ("⚠", "$warning", "bold italic"),
    "cancelling": ("⧗", "$warning", "bold italic"),
    "cancelled": ("─", "$hint", "dim"),
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
    """Every canonical state returns its `(glyph, color, text_style)`.

    qol-084: no ``DeprecationWarning`` fires on any lookup because the
    legacy-alias branch was removed — `resolve_state` is a plain
    ``STATE_TOKENS[name]``.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        token = resolve_state(name)
    assert isinstance(token, StateToken)
    assert (token.glyph, token.color, token.text_style) == expected


def test_state_token_is_frozen_dataclass() -> None:
    """`StateToken` is immutable — enables safe module-level reuse."""
    token = resolve_state("success")
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError subclass
        token.glyph = "?"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# qol-084: legacy aliases are gone; wire-vocab translation is a distinct map
# ---------------------------------------------------------------------------


_LEGACY_NAMES: tuple[str, ...] = (
    "completed",
    "error",
    "initializing",
    "starting",
    "pruned",
)


@pytest.mark.parametrize("legacy", _LEGACY_NAMES)
def test_legacy_state_names_raise_key_error(legacy: str) -> None:
    """qol-084: `_LEGACY_ALIASES` deleted — historical names no longer resolve."""
    with pytest.raises(KeyError):
        resolve_state(legacy)


def test_resolve_state_does_not_emit_deprecation_warning() -> None:
    """qol-084: the DeprecationWarning branch is gone — no callsite warns."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for name in STATE_TOKENS:
            resolve_state(name)
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert dep == [], f"unexpected DeprecationWarning(s): {[str(w.message) for w in dep]}"


# ---------------------------------------------------------------------------
# WIRE_TO_CANONICAL — the two-entry wire→canonical translation table
# ---------------------------------------------------------------------------


def test_wire_to_canonical_covers_the_two_persistent_wire_names() -> None:
    """The wire vocab from ``tools/services/cards.py`` still uses
    ``"error"`` and ``"initializing"``; these are the only two names
    the widget boundary must translate. Every other canonical name
    is a wire-canonical-identity pair.
    """
    assert WIRE_TO_CANONICAL == {"error": "failed", "initializing": "queued"}


@pytest.mark.parametrize(
    ("wire", "canonical"),
    list(WIRE_TO_CANONICAL.items()),
)
def test_wire_translation_lands_on_a_canonical_state(
    wire: str,
    canonical: str,
) -> None:
    """Each wire→canonical entry maps to a real STATE_TOKENS key."""
    assert canonical in STATE_TOKENS
    # Sanity: the wire name itself is NOT canonical (that's the whole
    # reason it needs translation).
    assert wire not in STATE_TOKENS


# ---------------------------------------------------------------------------
# Unknown-name behavior
# ---------------------------------------------------------------------------


def test_unknown_state_raises_key_error() -> None:
    """Unrecognised names — hard fail with a `KeyError`."""
    with pytest.raises(KeyError):
        resolve_state("nonsense")


def test_unknown_state_does_not_emit_deprecation_warning() -> None:
    """qol-084: no branch of `resolve_state` warns anymore."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(KeyError):
            resolve_state("nonsense")
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert dep == []
