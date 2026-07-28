"""spec-005 Test 1 — `Card` Protocol + `AbstractCard` shape.

The design principle: `AbstractCard` does NOT inherit from `Card`.
Structural typing carries the Protocol shape at every use site so
the ABC stays a plain no-op base for tests + platform impls. The
`__mro__` assert pins the design so a future refactor cannot silently
flip to `AbstractCard(Card)`.

The stub-raise asserts pin the other half: each `AbstractCard` method
raises `NotImplementedError` by default, so a test subclass that
forgets to override an entry gets a loud failure instead of a silent
no-op.
"""

from __future__ import annotations

import inspect
from types import TracebackType

import pytest

from tools.services import AbstractCard, Card
from tools.services.cards import CardFinalStatus, CardStatus


class _CardStub(AbstractCard):
    """Trivial concrete subclass so we can instantiate the ABC."""


def test_card_protocol_not_in_abstract_card_mro() -> None:
    """Structural typing — the ABC must NOT inherit from the Protocol.

    spec-005 AC1 + spec-001 pattern. A future refactor accidentally
    switching to `AbstractCard(Card)` would silently break the
    Protocol / ABC split; this assert catches that.
    """
    assert Card not in AbstractCard.__mro__


def test_abstract_card_instantiates_and_defaults_owner_transferred_false() -> None:
    """A trivial subclass instantiates; `_owner_transferred` defaults `False`.

    The class-level default set on `AbstractCard` propagates to
    instances — the workflow-service (spec-017) is the only in-tree
    writer that flips it to `True`.
    """
    stub = _CardStub()
    assert stub._owner_transferred is False


def test_abstract_card_owner_transferred_writable_to_true() -> None:
    """Writing `True` to `_owner_transferred` succeeds; the ABC does not enforce immutability."""
    stub = _CardStub()
    stub._owner_transferred = True
    assert stub._owner_transferred is True


@pytest.mark.asyncio
async def test_abstract_card_methods_raise_not_implemented() -> None:
    """Every default async method raises `NotImplementedError`.

    Concrete impls (spec-006) override each one; the ABC's job is to
    fail loudly for incomplete impls / test mocks that forgot a
    method.
    """
    stub = _CardStub()
    with pytest.raises(NotImplementedError):
        await stub.append("chunk")
    with pytest.raises(NotImplementedError):
        await stub.set("content")
    with pytest.raises(NotImplementedError):
        await stub.set_progress(1, 10)
    with pytest.raises(NotImplementedError):
        await stub.set_status("running")
    with pytest.raises(NotImplementedError):
        await stub.set_title("new")
    with pytest.raises(NotImplementedError):
        await stub.close()
    with pytest.raises(NotImplementedError):
        _ = stub.is_closed
    with pytest.raises(NotImplementedError):
        await stub.__aenter__()


@pytest.mark.asyncio
async def test_abstract_card_aexit_raises_not_implemented() -> None:
    """`__aexit__(None, None, None)` raises `NotImplementedError`."""
    stub = _CardStub()
    with pytest.raises(NotImplementedError):
        await stub.__aexit__(None, None, None)


def test_abstract_card_aexit_signature_fully_annotated() -> None:
    """`__aexit__` params are fully typed — no `Any` fallback (AC 11).

    spec-005 requires `type[BaseException] | None`,
    `BaseException | None`, `TracebackType | None` — matching the
    Protocol declaration.
    """
    sig = inspect.signature(AbstractCard.__aexit__)
    # self + three params
    params = list(sig.parameters.values())
    assert len(params) == 4  # self, exc_type, exc_val, tb
    # Grab the three-arg triple after `self`.
    exc_type_param, exc_val_param, tb_param = params[1], params[2], params[3]
    # get_type_hints resolves the string annotations under
    # `from __future__ import annotations`.
    hints = inspect.get_annotations(AbstractCard.__aexit__, eval_str=True)
    assert hints[exc_type_param.name] == (type[BaseException] | None)
    assert hints[exc_val_param.name] == (BaseException | None)
    assert hints[tb_param.name] == (TracebackType | None)


def test_card_status_literal_members() -> None:
    """`CardStatus` covers every state a card can hold.

    Includes the intermediate `queued` / `cancelling` states —
    `WorkflowsImpl.set_status("cancelling")` and the `queued` phase
    are real callers, and `card_kinds.header_glyph_by_status` renders
    a glyph for each. (Previously the Literal was missing both; mypy
    surfaced the drift once the gate's type step ran.)
    """
    from typing import get_args

    members = set(get_args(CardStatus))
    assert members == {
        "queued",
        "running",
        "cancelling",
        "success",
        "error",
        "cancelled",
    }


def test_card_final_status_literal_members() -> None:
    """`CardFinalStatus` is exactly the terminal subset."""
    from typing import get_args

    members = set(get_args(CardFinalStatus))
    assert members == {"success", "error", "cancelled"}
