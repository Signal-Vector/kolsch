"""Card-kind registry — v1 closed set of card kinds + per-kind metadata.

kolsch renders every card handle through a closed-set kind registry.
Each entry declares:

- `name` — the string tools pass to
  [`Cards.open`][tools.services.cards.Cards.open].
- `allowed_ops` — the frozenset of `Card` handle methods valid for
  this kind. [`CardsImpl`][tools.services.cards] (spec-006) gates
  every mutation against this set and raises `TypeError` on a miss.
- `default_status` — the [`CardStatus`][tools.services.cards.CardStatus]
  a freshly-opened card carries.
- `header_glyph_by_status` — mapping used by the widget layer
  (spec-007+) to pick the glyph shown in the card header.

The registry lives ONCE (this file). Bundle authors cannot add kinds
in v1 — the closed set is a v1 invariant so the widget layer can
compile-time reason about the render path.

Use [`get_kind_spec`][tools.services.card_kinds.get_kind_spec] to
resolve a kind by name; it raises
[`UnknownCardKind`][tools.services.exceptions.UnknownCardKind] on a
miss with a message pointing to the issue template so tool authors
have an actionable next step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tools.services.exceptions import UnknownCardKind

CardKindLiteral = Literal["markdown", "progress", "confirm", "workflow"]
"""The closed set of v1 card kinds.

Tools annotate `kind` arguments with this alias so mypy catches
typos at authoring time. Runtime callers still route through
[`get_kind_spec`][tools.services.card_kinds.get_kind_spec] to
resolve the spec + raise
[`UnknownCardKind`][tools.services.exceptions.UnknownCardKind]
on a bad string reaching the impl.
"""


@dataclass(frozen=True)
class CardKindSpec:
    """Frozen metadata for one entry in the card-kind registry.

    Instances live in
    [`CARD_KIND_REGISTRY`][tools.services.card_kinds.CARD_KIND_REGISTRY]
    keyed by `name`. Frozen so accidental mutation is a loud failure
    rather than a silent registry corruption.
    """

    name: str
    allowed_ops: frozenset[str]
    default_status: Literal["running", "success"]
    header_glyph_by_status: dict[str, str]


CARD_KIND_REGISTRY: dict[str, CardKindSpec] = {
    "markdown": CardKindSpec(
        name="markdown",
        allowed_ops=frozenset(
            {
                "append",
                "set",
                "set_status",
                "set_title",
                "close",
            },
        ),
        default_status="running",
        header_glyph_by_status={
            "running": "⠋",
            "success": "✓",
            "error": "✗",
            "cancelled": "─",
        },
    ),
    "progress": CardKindSpec(
        name="progress",
        allowed_ops=frozenset(
            {
                "set_progress",
                "set_status",
                "set_title",
                "close",
            },
        ),
        default_status="running",
        header_glyph_by_status={
            "running": "▸",
            "success": "✓",
            "error": "✗",
            "cancelled": "─",
        },
    ),
    "confirm": CardKindSpec(
        name="confirm",
        # r1 product cut removed prompt_choice sugar — tools use
        # Prompts.choice directly against the card_id.
        allowed_ops=frozenset({"set", "close"}),
        default_status="running",
        header_glyph_by_status={
            "running": "⚠",
            "success": "✓",
            "error": "✗",
            "cancelled": "─",
        },
    ),
    "workflow": CardKindSpec(
        name="workflow",
        # r3 BLOCKER fix — set_progress required by the WorkflowRenderer
        # header progress bar in spec-013.
        allowed_ops=frozenset(
            {
                "append",
                "set",
                "set_progress",
                "set_status",
                "set_title",
                "close",
            },
        ),
        default_status="running",
        header_glyph_by_status={
            "queued": "⋯",
            "running": "▸",
            "cancelling": "⧗",
            "success": "✓",
            "error": "✗",
            "cancelled": "─",
        },
    ),
}


def get_kind_spec(kind: str) -> CardKindSpec:
    """Resolve a card kind by name.

    Args:
        kind: The kind name a caller passed to
            [`Cards.open`][tools.services.cards.Cards.open]. mypy
            catches typos via
            [`CardKindLiteral`][tools.services.card_kinds.CardKindLiteral];
            runtime miss reaches this function.

    Returns:
        The frozen
        [`CardKindSpec`][tools.services.card_kinds.CardKindSpec]
        registered for `kind`.

    Raises:
        UnknownCardKind: `kind` is not in
            [`CARD_KIND_REGISTRY`][tools.services.card_kinds.CARD_KIND_REGISTRY].
            The message includes the sorted list of known kinds and a
            pointer to the issue template so the caller has an
            actionable next step.
    """
    spec = CARD_KIND_REGISTRY.get(kind)
    if spec is None:
        known = sorted(CARD_KIND_REGISTRY)
        msg = (
            f"Unknown card kind: {kind!r}. Known: {known}. See "
            f"docs/reference/card-kinds.md for the catalog "
            f"(coming with feat-009); file an issue at "
            f"https://github.com/Signal-Vector/kolsch/issues/new "
            f"to request a new kind."
        )
        raise UnknownCardKind(msg)
    return spec
