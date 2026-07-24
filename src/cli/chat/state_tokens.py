"""Canonical chat-widget state → `(glyph, color, text_style)` table.

Single source of truth for the state vocabulary used across the chat
surface. Replaces the two divergent per-widget glyph maps that
historically disagreed on state *names* for the same visual meaning
(``completed`` vs ``success``, ``failed`` vs ``error``, ``starting``
vs ``initializing``, ``pruned`` vs ``cancelled``). See qol-075 for
the unification and qol-084 for the migration of the widgets'
reactive vocabulary to canonical (which dissolved the legacy alias
bridge that qol-075 shipped as a one-release stopgap).

Widgets look up tokens via :func:`resolve_state`. Unknown names raise
:class:`KeyError` — callers that receive wire vocab (e.g. the
``CardFinalStatus`` literal from ``tools/services/cards.py`` still
uses ``"error"``) MUST translate at the widget boundary via
:data:`WIRE_TO_CANONICAL` before storing on their reactive.

Color values are palette *variable names* (e.g. ``"$tool-fg"``) so
downstream widget CSS keeps controlling the actual RGB. Text-style
values are Textual ``text-style`` strings (``"bold"``, ``"dim"``,
``"bold italic"``, or ``""`` for the default).
"""

from __future__ import annotations

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
    "success": StateToken("✓", "$tool-fg", "bold"),
    "failed": StateToken("✗", "$error", "bold"),
    "running": StateToken("▸", "$tool-fg", "bold"),
    "queued": StateToken("⋯", "$hint", "dim"),
    "paused": StateToken("⚠", "$warning", "bold italic"),
    "awaiting": StateToken("⚠", "$warning", "bold italic"),
    "cancelling": StateToken("⧗", "$warning", "bold italic"),
    "cancelled": StateToken("─", "$hint", "dim"),
}
"""Canonical state → token map.

The eight canonical names are the vocabulary every chat widget uses
internally. Callers passing state strings to widget APIs
(``set_state``, ``apply_status``) must use these names; the
:data:`WIRE_TO_CANONICAL` map handles the one-shot translation
from tools-side wire vocab (``"error"``, ``"initializing"``) that
enters at the ``CardWidget.apply_close`` / ``apply_status`` boundary.
"""


WIRE_TO_CANONICAL: dict[str, str] = {
    "error": "failed",
    "initializing": "queued",
}
"""Tools-wire state name → canonical widget state name.

The wire vocab (``CardFinalStatus`` / ``CardStatus`` in
``src/tools/services/cards.py``) still uses ``"error"`` and
``"initializing"`` because renaming those types ripples across
every tool author and every card-related test outside the chat
package — that's a distinct migration.

At the two chat widget entry points that receive wire vocab
(``CardWidget.apply_close``, ``CardWidget.apply_status``,
plus the spec-006 ``_CardMountStub`` mirror in
``cli/chat/event_adapter.py``) callers translate via
``WIRE_TO_CANONICAL.get(name, name)`` before storing on the
reactive ``state``. Non-mapped names pass through — most terminal
statuses (``"success"``, ``"cancelled"``) are already canonical.
"""


def resolve_state(name: str) -> StateToken:
    """Look up a :class:`StateToken` by canonical name.

    Unknown names raise :class:`KeyError`. Since qol-084 there is no
    legacy-alias fallback — callers holding wire vocab must translate
    via :data:`WIRE_TO_CANONICAL` first.
    """
    return STATE_TOKENS[name]


__all__ = ["STATE_TOKENS", "WIRE_TO_CANONICAL", "StateToken", "resolve_state"]
