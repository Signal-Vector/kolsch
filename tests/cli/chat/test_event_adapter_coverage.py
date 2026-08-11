"""Test 5 — event coverage smoke.

Drives one canonical payload of each ``EventType`` through
``EventAdapter.handle`` and asserts no exception. Missing branch =
static failure (``KeyError`` from the dispatch table lookup).

Fixture: ``canonical_event`` from ``tests/cli/chat/conftest.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from cli.chat.event_adapter import EventAdapter
from schemas.events import EventType


class _MockChatApp:
    """Minimal ChatApp double for the adapter's public surface.

    Every method the adapter calls resolves to a no-op / trivial
    coroutine. The adapter is expected to be defensive against
    missing widgets; this double keeps the surface even smaller so
    a missing dispatch branch surfaces cleanly.
    """

    def __init__(self) -> None:
        self.notices: list[str] = []
        self._last_assistant: str = ""

    async def mount_user_message(
        self,
        text: str,
        timestamp: Any = None,
        *,
        event_id: Any = None,
        content: Any = None,
    ) -> None:
        _ = timestamp, event_id, content
        self.notices.append(f"user:{text}")

    async def mount_assistant_message(
        self,
        text: str,
        *,
        event_id: Any = None,
    ) -> None:
        _ = event_id
        self.notices.append(f"assistant:{text}")

    async def mount_assistant_streaming_widget(self) -> Any:
        class _MockProse:
            async def append(self, text: str) -> None:
                return None

        return _MockProse()

    async def mount_tool_call(self, *args: Any, **kwargs: Any) -> None:
        _ = kwargs
        self.notices.append(f"tool:{args}")

    async def mount_tool_call_running(self, *args: Any, **kwargs: Any) -> Any:
        # spec-130 Step 5: `_handle_tool_called` calls this directly
        # (not through the `ChatMountSink` Protocol) to mount the
        # running/pending block. Same no-op-with-notice shape as
        # `mount_tool_call` above — this double only needs to satisfy
        # the call, not track pending-block identity.
        _ = kwargs
        self.notices.append(f"tool_running:{args}")
        return None

    async def clear_working_placeholder(self) -> None:
        # spec-130 Step 5 (fixup): `_handle_tool_called` calls this
        # directly for card-mounting tool calls. The canonical
        # TOOL_CALLED payload (`tests/cli/chat/conftest.py`) uses
        # `read_file`, which is never card-mounting, so this branch
        # isn't exercised by this sweep today — present for symmetry/
        # forward-compat with the rest of the double's App surface.
        self.notices.append("placeholder_cleared")

    async def mount_notice(
        self,
        notice_id: str = "",
        text: str = "",
        *,
        level: str = "info",
        event_id: Any = None,
    ) -> None:
        _ = notice_id, level, event_id
        self.notices.append(f"notice:{text}")

    async def mount_exchange_failed(self, message: str) -> None:
        self.notices.append(f"failed:{message}")

    async def mount_approval_block(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def set_turn_verb(self, verb: str, *, target: str = "") -> None:
        return None

    def clear_awaiting_approval(self) -> None:
        return None

    def last_assistant_text(self) -> str:
        return self._last_assistant

    def set_last_assistant_text(self, text: str) -> None:
        self._last_assistant = text


@pytest.mark.parametrize("event_type", list(EventType))
async def test_adapter_handles_every_event_type(
    event_type: EventType,
    canonical_event: Any,
) -> None:
    """Every ``EventType`` member must dispatch without raising.

    Static check first (``EventAdapter.handles``); dynamic check
    via ``adapter.handle(event)`` with a canonical payload.
    """
    assert EventAdapter.handles(event_type), (
        f"event_adapter.py has no branch for {event_type}"
    )
    event = canonical_event(event_type)
    app = _MockChatApp()
    adapter = EventAdapter(app)
    await adapter.handle(event)
