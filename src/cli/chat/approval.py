"""Inline ``PromptCallback`` — mounts ``ApprovalBlock`` and awaits resolution.

The chat lifecycle registers this callable as the authz provider's
prompt callback via ``authz.register_prompt_callback`` (see
``src/providers/authz.py:1209``). When the callback fires from a
worker thread, it schedules an ``ApprovalBlock`` mount on the
Textual event loop via ``app.call_from_thread``, then awaits the
block's ``asyncio.Event`` for the user's resolution and returns
the tuple the authz provider expects
(``PromptCallbackReturn = AuthzEffect | tuple[AuthzEffect, Rule]``
— see ``src/cli/_modal.py:168``).

The adapter (``event_adapter.py``) does not know approvals exist;
this module is the single carve-out to the "renderer is a pure
event consumer" invariant.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from providers.authz import AuthzEffect, Rule

if TYPE_CHECKING:
    from cli.chat.app import ChatApp


PromptCallbackReturn = AuthzEffect | tuple[AuthzEffect, Rule]


class InlineApprovalCallback:
    """Mount ``ApprovalBlock`` inline and await its resolution.

    Constructed once per chat session (in ``ChatApp.on_mount``) and
    registered with the runtime's authz provider. The callback is
    called with keyword args ``rule_id``, ``action``, ``target`` (see
    ``src/providers/authz.py:319``).
    """

    def __init__(self, app: ChatApp) -> None:
        self._app = app

    async def __call__(
        self,
        *,
        rule_id: str | None,
        action: str,
        target: str | None,
        caller_chain: tuple[str, ...] = (),
    ) -> PromptCallbackReturn:
        """Mount an ``ApprovalBlock`` and await the user's choice.

        The block's ``resolved()`` coroutine returns the
        ``PromptCallbackReturn`` tuple; we simply forward it. The
        block stays mounted after resolution (mount-and-freeze).

        ``caller_chain`` is accepted for signature compatibility with
        the ``PromptCallback`` Protocol but not consumed by v1 — the
        block's frozen footer is enough for the user; the chain lives
        on the JSONL for later analysis.
        """
        _ = caller_chain
        loop = asyncio.get_event_loop()
        # The callback executes on the same event loop that runs the
        # Textual app (the framework runs its LLM handles as awaitables
        # inside the app's loop). Mount directly.
        block = await self._app.mount_approval_block(rule_id, action, target)
        try:
            result = await block.resolved()
        finally:
            # Composer disables while awaiting approval; re-enable on
            # any exit path (including cancellation).
            self._app.clear_awaiting_approval()
        _ = loop
        return result


__all__ = ["InlineApprovalCallback", "PromptCallbackReturn"]
