"""Cards service — streaming progress cards + short-lived notices.

Two shapes:

- [`Cards`][tools.services.cards.Cards] is the author-facing Protocol
  tools annotate against for autocomplete + typecheck at use sites.
  `@runtime_checkable` is deliberately NOT set (default `False`) — all
  narrowing goes through
  [`require_services`][tools.services.require_services], never
  `isinstance`.
- [`AbstractCards`][tools.services.cards.AbstractCards] is a plain ABC
  whose methods raise `NotImplementedError`. Tests subclass it to
  build stub impls; concrete impls (spec-005) subclass it and override
  the stubs.

The ABC does NOT inherit from the Protocol — structural typing
carries the shape at every use site. Test 4 (from spec-001) asserts
`Cards not in AbstractCards.__mro__` for exactly this reason.
"""

from __future__ import annotations

from abc import ABC
from typing import Any, Protocol


class Cards(Protocol):
    """Tools' author-facing view of the cards service."""

    async def open(self, title: str, kind: str = "markdown") -> Any:  # noqa: ANN401
        """Open a streaming card. Returns a card handle (typed in spec-005)."""
        ...

    def notice(self, text: str, *, kind: str = "info") -> None:
        """Emit a short-lived notice card that closes itself."""
        ...


class AbstractCards(ABC):
    """Platform + test base for `Cards` impls. Methods raise by default."""

    async def open(self, title: str, kind: str = "markdown") -> Any:  # noqa: ANN401
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError

    def notice(self, text: str, *, kind: str = "info") -> None:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Called from ChatApp.on_unmount. Cancels drain tasks, snapshot timers, cancel tokens. Idempotent. Post-aclose, subsequent mutation raises RuntimeError.

        The ABC default is a no-op — test fakes and the placeholder
        `ChatServiceRegistry` fields (all `None` today) don't need real
        teardown. Concrete impls in spec-006 override with real
        teardown logic.
        """
        return None
