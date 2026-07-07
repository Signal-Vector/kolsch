"""spec-005 Test 3 — `Cards.open()` signature accepts `CardKindLiteral`.

spec-001 shipped `Cards.open(title, kind: str = "markdown")`; this
spec tightens the `kind` param to `CardKindLiteral` so mypy catches
typos at author time. Runtime callers still route through
`get_kind_spec` which raises `UnknownCardKind` on a bad string.
"""

from __future__ import annotations

import inspect

from tools.services import AbstractCards, Cards
from tools.services.card_kinds import CardKindLiteral


def test_cards_protocol_open_kind_annotation_is_card_kind_literal() -> None:
    """The Protocol's `kind` param is typed as `CardKindLiteral`."""
    hints = inspect.get_annotations(Cards.open, eval_str=True)
    assert hints["kind"] == CardKindLiteral


def test_cards_protocol_open_kind_default_is_markdown() -> None:
    """`kind` defaults to `"markdown"` — the safest, most-used kind."""
    sig = inspect.signature(Cards.open)
    assert sig.parameters["kind"].default == "markdown"


def test_abstract_cards_open_kind_annotation_matches() -> None:
    """`AbstractCards.open` signature mirrors the Protocol (AC 6)."""
    hints = inspect.get_annotations(AbstractCards.open, eval_str=True)
    assert hints["kind"] == CardKindLiteral


def test_abstract_cards_open_kind_default_matches() -> None:
    """`AbstractCards.open` default matches the Protocol default."""
    sig = inspect.signature(AbstractCards.open)
    assert sig.parameters["kind"].default == "markdown"
