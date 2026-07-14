"""``CancelToken`` wiring for the Textual chat surface.

The legacy REPL routed ``Esc`` and ``Ctrl-C`` into a single
``CancelToken`` (see ``src/cli/chat/_legacy.py:565``
``_watch_esc_during_streaming``). The Textual surface achieves the
same via a keybinding on ``escape`` / ``ctrl+c`` that flips the
per-exchange ``CancelToken`` stored on the app; no per-turn
background stdin watcher is needed because Textual's own event loop
delivers keys to the running app.

This module owns the ``CancelToken`` construction + accessor helper
so ``keybindings.py`` and ``app.py`` don't reach into each other's
internals for the state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engine import CancelToken

if TYPE_CHECKING:
    from cli.chat.app import ChatApp


def new_cancel_token() -> CancelToken:
    """Construct a fresh ``CancelToken`` for a new exchange."""
    return CancelToken()


def request_cancel(app: ChatApp, *, reason: str = "interrupted") -> None:
    """Cancel the active exchange, if any.

    Idempotent — a second call is a no-op. Called from the
    keybindings' ``escape`` and ``ctrl+c`` handlers.
    """
    token = getattr(app, "active_cancel", None)
    if token is None:
        return
    try:
        token.cancel()
    except Exception:
        # A cancellation on an already-cancelled token surfaces as
        # noise; silence it. The engine's teardown is the source of
        # truth.
        pass
    app.last_cancel_reason = reason


__all__ = ["CancelToken", "new_cancel_token", "request_cancel"]
