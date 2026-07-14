"""kolsch chat surface — Textual (inline mode) UI.

Public API: :class:`ChatApp` — the Textual ``App`` shell mounted by
:mod:`cli.main` for ``kolsch chat``. Everything else (widgets,
adapters, session helpers, approval callback) is a submodule accessed
directly (``cli.chat.session``, ``cli.chat.widgets.user_chatbox``, …).
"""

from __future__ import annotations

from cli.chat.app import ChatApp

__all__ = ["ChatApp"]
