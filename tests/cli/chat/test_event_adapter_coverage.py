"""Test 5 — event coverage smoke.

Drives one canonical payload of each ``EventType`` through
``EventAdapter.handle`` and asserts no exception. Missing branch =
static failure (``KeyError`` from the dispatch table lookup).

Fixture: ``canonical_event`` from ``tests/cli/chat/conftest.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from cli.chat.event_adapter import EventAdapter
from schemas.events import EventType

if TYPE_CHECKING:
    pass


class _FakeChatApp:
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
        self, text: str, timestamp: Any = None,
        *, event_id: Any = None,
    ) -> None:
        _ = timestamp, event_id
        self.notices.append(f"user:{text}")

    async def mount_assistant_message(
        self, text: str, *, event_id: Any = None,
    ) -> None:
        _ = event_id
        self.notices.append(f"assistant:{text}")

    async def mount_assistant_streaming_widget(self) -> Any:
        class _FakeProse:
            async def append(self, text: str) -> None:
                return None

        return _FakeProse()

    async def mount_tool_call(self, *args: Any, **kwargs: Any) -> None:
        _ = kwargs
        self.notices.append(f"tool:{args}")

    async def mount_notice(
        self, notice_id: str = "", text: str = "",
        *, level: str = "info", event_id: Any = None,
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
    app = _FakeChatApp()
    adapter = EventAdapter(app)
    await adapter.handle(event)
