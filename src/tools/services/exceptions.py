"""Exceptions raised by tool-services helpers and impls.

These three exception classes are the wire-level failure shapes tool
authors handle when a requested service is missing, a prompt slot is
already occupied, or an open card was closed out from under them.
Concrete impls (arriving in later specs) raise these; the scaffold
declares them so the shapes are frozen from spec-001 onward.
"""

from __future__ import annotations


class ToolNotAvailable(Exception):
    """A tool declared `requires=[...]` but the service is not wired."""


class PromptBusy(Exception):
    """A prompt slot is already occupied by another source."""


class CardClosed(Exception):
    """The card handle was closed and further mutation is rejected."""
