"""Exceptions raised by tool-services helpers and impls.

These exception classes are the wire-level failure shapes tool authors
handle when a requested service is missing, a prompt slot is already
occupied, an open card was closed out from under them, or a caller
asks for a card kind the platform does not know about. Concrete impls
(arriving in later specs) raise these; the scaffold declares them so
the shapes are frozen from spec-001 onward.
"""

from __future__ import annotations


class ToolNotAvailable(Exception):
    """A tool declared `requires=[...]` but the service is not wired."""


class PromptBusy(Exception):
    """A prompt slot is already occupied by another source."""


class CardClosed(Exception):
    """The card handle was closed and further mutation is rejected."""


class UnknownCardKind(Exception):
    """The requested card kind is not in the v1 closed-set registry.

    Raised by
    [`get_kind_spec`][tools.services.card_kinds.get_kind_spec] on a
    miss. The message includes the sorted list of known kinds and a
    pointer to the issue template for requesting a new kind so tool
    authors have an actionable next step. The registry is
    intentionally closed in v1 — bundle authors cannot add kinds
    until the roadmap opens the door in v1.1.
    """
