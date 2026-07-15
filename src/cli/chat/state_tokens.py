"""Canonical chat-widget state → `(glyph, color, text_style)` table.

Single source of truth for the state vocabulary used across the chat
surface. Replaces the two divergent per-widget glyph maps that
historically disagreed on state *names* for the same visual meaning
(``completed`` vs ``success``, ``failed`` vs ``error``, ``starting``
vs ``initializing``, ``pruned`` vs ``cancelled``). See qol-075.

Widgets look up tokens via :func:`resolve_state`. Legacy names still
resolve — with a :class:`DeprecationWarning` — via
:data:`_LEGACY_ALIASES`; the aliases exist as a one-release bridge
and are expected to be removed once every emitter is migrated.

Color values are palette *variable names* (e.g. ``"$tool-fg"``) so
downstream widget CSS keeps controlling the actual RGB. Text-style
values are Textual ``text-style`` strings (``"bold"``, ``"dim"``,
``"bold italic"``, or ``""`` for the default).
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass


@dataclass(frozen=True)
class StateToken:
    """A rendered state's glyph + color + text-style triple.

    - ``glyph`` — the Unicode leading character for the state
      (``✓``, ``✗``, ``▸``, ``⋯``, ``⚠``, ``⧗``, ``─``). While a
      widget is *animating* a state (see ``_ANIMATED_STATES`` in
      ``workflow_node_row.py``) the spinner-frame table overrides
      this glyph — the token's ``glyph`` is the resting/static
      character.
    - ``color`` — palette variable name (``"$tool-fg"``, ``"$error"``,
      ``"$warning"``, ``"$hint"``). Widgets already declare CSS rules
      keyed on state class; this field exists so downstream primitives
      (see spec-078's gutter) can render outside of a per-widget CSS
      block.
    - ``text_style`` — Textual ``text-style`` string. Empty string
      means "no override" (the widget's default takes effect).
    """

    glyph: str
    color: str
    text_style: str


STATE_TOKENS: dict[str, StateToken] = {
    "success":    StateToken("✓", "$tool-fg", "bold"),
    "failed":     StateToken("✗", "$error",   "bold"),
    "running":    StateToken("▸", "$tool-fg", "bold"),
    "queued":     StateToken("⋯", "$hint",    "dim"),
    "paused":     StateToken("⚠", "$warning", "bold italic"),
    "awaiting":   StateToken("⚠", "$warning", "bold italic"),
    "cancelling": StateToken("⧗", "$warning", "bold italic"),
    "cancelled":  StateToken("─", "$hint",    "dim"),
}
"""Canonical state → token map.

The eight canonical names are the vocabulary every chat widget
should use. New callers must key off these names; legacy callers go
through :func:`resolve_state` which translates via
:data:`_LEGACY_ALIASES`.
"""


_LEGACY_ALIASES: dict[str, str] = {
    "completed":    "success",
    "error":        "failed",
    # WorkflowNodeRow's ``starting`` renders the animated spinner
    # (see ``_ANIMATED_STATES``), i.e. reads as "in motion" —
    # ``running`` is the correct canonical bucket.
    "starting":     "running",
    # CardWidget's ``initializing`` renders ``⠋`` as a *resting* glyph
    # (no spinner timer on the header), i.e. reads as
    # "not-yet-started, being prepared" — ``queued`` is the calmer
    # canonical bucket. The visible glyph changes ``⠋`` → ``⋯``, but
    # the current styleguide inventory shows ``initializing`` never
    # co-occurs with a resolve site: card widgets look up header
    # glyphs via the kind-spec map first and only fall back to the
    # generic map for out-of-kind states (``paused`` on a markdown
    # card, etc.). The visible-behavior contract is preserved.
    "initializing": "queued",
    "pruned":       "cancelled",
}
"""Legacy state name → canonical state name.

Retained as a one-release bridge so external emitters (engine wire
events, older UI event fixtures) keep resolving. Every hit routed
through :func:`resolve_state` emits a
:class:`DeprecationWarning` — internal callers under
``src/cli/chat/`` are expected to key off canonical names directly.
"""


def resolve_state(name: str) -> StateToken:
    """Look up a :class:`StateToken` by canonical or legacy name.

    Canonical hits return in O(1) with no side effects. Legacy hits
    (``completed``, ``error``, ``initializing``, ``starting``,
    ``pruned``) emit a :class:`DeprecationWarning` naming the
    canonical replacement and then return that canonical token.
    Unknown names raise :class:`KeyError`.
    """
    token = STATE_TOKENS.get(name)
    if token is not None:
        return token
    canonical = _LEGACY_ALIASES.get(name)
    if canonical is not None:
        warnings.warn(
            f"state name {name!r} is deprecated; use {canonical!r}",
            DeprecationWarning,
            stacklevel=2,
        )
        return STATE_TOKENS[canonical]
    raise KeyError(f"unknown state: {name!r}")


__all__ = ["StateToken", "STATE_TOKENS", "resolve_state"]
