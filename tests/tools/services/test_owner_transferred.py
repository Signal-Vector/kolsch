"""spec-005 Test 4 — `_owner_transferred` on Protocol + ABC.

The workflow-service (spec-017) sets `_owner_transferred = True` when
it takes over a card's terminal transition (`Workflows.start(card=)`
overload). This test pins:

- The Protocol declares the attribute (so mypy accepts writes on a
  `Card`-typed variable).
- `AbstractCard` initializes to `False` at the class level.
- Writing `True` succeeds — the ABC does not enforce immutability on
  the flag.
"""

from __future__ import annotations

from tools.services import AbstractCard, Card


class _CardStub(AbstractCard):
    """Trivial concrete subclass so we can instantiate the ABC."""


def test_abstract_card_owner_transferred_defaults_false() -> None:
    """Class-level default `False` propagates to instances."""
    stub = _CardStub()
    assert stub._owner_transferred is False


def test_abstract_card_owner_transferred_is_writable() -> None:
    """Writing `True` succeeds; subsequent reads return the new value."""
    stub = _CardStub()
    assert stub._owner_transferred is False
    stub._owner_transferred = True
    assert stub._owner_transferred is True


def test_card_protocol_declares_owner_transferred_attribute() -> None:
    """The Protocol declares `_owner_transferred: bool` (mypy contract).

    A `Card`-typed variable can be assigned to on this attribute —
    verified by presence + `bool` annotation on the Protocol's
    `__annotations__` (the static-typing surface consumers rely on).
    `from __future__ import annotations` stores the annotation as
    the string form, so this test pins the string. mypy resolves it
    the same way when it typechecks assignments.
    """
    assert "_owner_transferred" in Card.__annotations__
    assert Card.__annotations__["_owner_transferred"] == "bool"
