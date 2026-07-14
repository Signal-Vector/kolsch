"""Cards service — streaming progress cards + short-lived notices.

Two shapes for the service surface plus the `Card` handle:

- [`Cards`][tools.services.cards.Cards] is the author-facing Protocol
  tools annotate against for autocomplete + typecheck at use sites.
  `@runtime_checkable` is deliberately NOT set (default `False`) — all
  narrowing goes through
  [`require_services`][tools.services.require_services], never
  `isinstance`.
- [`AbstractCards`][tools.services.cards.AbstractCards] is a plain ABC
  whose methods raise `NotImplementedError`. Tests subclass it to
  build stub impls; concrete impls (spec-006) subclass it and override
  the stubs.
- [`Card`][tools.services.cards.Card] is the handle
  [`Cards.open`][tools.services.cards.Cards.open] returns. Tools
  mutate it through `append` / `set` / `set_progress` / `set_status`
  / `set_title` / `close`, or drive it as an async context manager
  (auto-close on exit).
- [`AbstractCard`][tools.services.cards.AbstractCard] is the ABC
  base for concrete `Card` impls and test mocks.

The ABCs do NOT inherit from the Protocols — structural typing
carries the shape at every use site. Test 4 (from spec-001) asserts
`Cards not in AbstractCards.__mro__`; the spec-005 protocol test
asserts the same for `Card` / `AbstractCard`.
"""

from __future__ import annotations

from abc import ABC
from types import TracebackType
from typing import TYPE_CHECKING, Literal, Protocol

from tools.services.card_kinds import CardKindLiteral

if TYPE_CHECKING:
    from schemas.identifiers import CardId


CardStatus = Literal["running", "success", "error", "cancelled"]
"""Every visible state a card can be in.

`running` is the default the widget layer paints while a tool is
still working; the three terminal states are what `close(final_status=...)`
accepts. `set_status` may drive through any of the four (the widget
layer picks the glyph via
[`CARD_KIND_REGISTRY`][tools.services.card_kinds.CARD_KIND_REGISTRY]).
"""

CardFinalStatus = Literal["success", "error", "cancelled"]
"""The terminal subset of [`CardStatus`][tools.services.cards.CardStatus].

`close(final_status=...)` requires a terminal — a card that closed
back to `running` is a bug shape the type system rules out at author
time.
"""


class Card(Protocol):
    """Handle to a mounted card. Tool-scoped by default.

    Every mutation method emits ONE UI_EVENT (via `CardsImpl`, which
    ships in spec-006). Ownership: the card belongs to the tool that
    opened it; auto-close on tool return (spec-008). Ownership can
    transfer to a service (e.g.
    [`Workflows.start(card=)`][tools.services.workflows.Workflows.start]
    in spec-017) which then owns the terminal transition. See
    [`_owner_transferred`][tools.services.cards.Card._owner_transferred].
    """

    id: CardId
    kind: str  # matches `kind` passed to Cards.open()
    title: str

    # r3 — private-by-convention attribute; workflow-service (spec-017)
    # is the only in-tree writer that sets it to True. AbstractCard
    # initializes False.
    _owner_transferred: bool

    # --- Content mutations (kind-dispatched under the hood) ---

    async def append(self, chunk: str) -> None:
        """Stream a chunk of content onto the card.

        Semantics by kind (enforced by `CardsImpl` in spec-006 via
        [`allowed_ops`][tools.services.card_kinds.CardKindSpec]):

        - `markdown`: append markdown text.
        - `progress`: not allowed — use `set_progress`.
        - `confirm`: not allowed.
        - `workflow`: append a log line to the current running node
          (see spec-013).
        """
        ...

    async def set(self, content: str) -> None:
        """Replace all content on the card."""
        ...

    async def set_progress(
        self,
        current: int,
        total: int,
        *,
        message: str = "",
    ) -> None:
        """Update progress.

        Only valid for `progress` and `workflow` kinds (workflow uses
        it to drive the header progress bar per r3 BLOCKER fix).
        """
        ...

    async def set_status(self, status: CardStatus) -> None:
        """Update the card's visual state (color, glyph)."""
        ...

    async def set_title(self, title: str) -> None:
        """Change the card's header title."""
        ...

    # --- Lifecycle ---

    async def close(
        self,
        *,
        collapse: bool = True,
        final_status: CardFinalStatus | None = None,
        error_message: str | None = None,
    ) -> None:
        """Mark the card as complete.

        Idempotent — a second `close` call silently succeeds so
        finalizer + explicit-close paths compose cleanly (spec-008).

        Args:
            collapse: `True` (default) collapses the card after
                close; `False` keeps it expanded.
            final_status: The terminal status to stamp. Defaults to
                the card's current status when `None`.
            error_message: Renders in the CardFooter when
                `final_status == "error"`; the widget layer truncates
                to 200 chars.
        """
        ...

    @property
    def is_closed(self) -> bool:
        """`True` once `close` has completed (or the finalizer ran).

        Consumers should treat `Card.is_closed` as a plain attribute
        — Protocols support `@property` declarations, and the concrete
        impl (spec-006) computes it from internal state rather than
        storing a bool.
        """
        ...

    # --- Context manager (auto-close on exit) ---

    async def __aenter__(self) -> Card:
        """Enter the card's async-context scope.

        Raises:
            CardClosed: The card is already closed (r3 footgun guard —
                re-entering a closed card is always a bug).
        """
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Auto-close with the appropriate terminal status.

        Mapping:

        - `exc_type is None` -> `final_status="success"`.
        - `exc_type is CancelledError` -> `final_status="cancelled"`.
        - Anything else -> `final_status="error"` +
          `error_message=str(exc_val)[:200]`.

        Never suppresses the exception (returns `None`, not `True`).
        """
        ...


class AbstractCard(ABC):
    """Stub base for concrete Card impls and test mocks.

    Does NOT inherit from `Card` Protocol (structural typing carries
    the shape at every use site — same pattern as
    [`AbstractCards`][tools.services.cards.AbstractCards]). Default
    methods raise `NotImplementedError` so incomplete impls fail
    loudly.
    """

    id: CardId
    kind: str
    title: str
    # r3 default — workflow-service (spec-017) writes True to transfer
    # ownership; the ABC does not enforce immutability on the flag.
    _owner_transferred: bool = False

    async def append(self, chunk: str) -> None:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError

    async def set(self, content: str) -> None:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError

    async def set_progress(
        self,
        current: int,
        total: int,
        *,
        message: str = "",
    ) -> None:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError

    async def set_status(self, status: CardStatus) -> None:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError

    async def set_title(self, title: str) -> None:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError

    async def close(
        self,
        *,
        collapse: bool = True,
        final_status: CardFinalStatus | None = None,
        error_message: str | None = None,
    ) -> None:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError

    @property
    def is_closed(self) -> bool:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError

    async def __aenter__(self) -> Card:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError


class Cards(Protocol):
    """Tools' author-facing view of the cards service."""

    async def open(
        self,
        title: str,
        kind: CardKindLiteral = "markdown",
    ) -> Card:
        """Open a streaming card. Returns the
        [`Card`][tools.services.cards.Card] handle the tool mutates.
        """
        ...

    def notice(
        self,
        text: str,
        *,
        kind: str = "info",
        card: Card | None = None,
    ) -> None:
        """Emit a short-lived notice.

        spec-008: when ``card`` is provided the notice mounts INSIDE
        that card's
        [`NoticeSlot`][cli.chat.widgets.notice_slot.NoticeSlot];
        otherwise it lands at the transcript root.
        """
        ...


class AbstractCards(ABC):
    """Platform + test base for `Cards` impls. Methods raise by default."""

    async def open(
        self,
        title: str,
        kind: CardKindLiteral = "markdown",
    ) -> Card:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError

    def notice(
        self,
        text: str,
        *,
        kind: str = "info",
        card: Card | None = None,
    ) -> None:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Called from ChatApp.on_unmount. Cancels drain tasks, snapshot timers, cancel tokens. Idempotent. Post-aclose, subsequent mutation raises RuntimeError.

        The ABC default is a no-op — test mocks and the placeholder
        `ChatServiceRegistry` fields (all `None` today) don't need real
        teardown. Concrete impls in spec-006 override with real
        teardown logic.
        """
        return None
