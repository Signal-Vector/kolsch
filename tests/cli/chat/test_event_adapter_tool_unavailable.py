"""feat-139 Test 4 — the chat adapter tolerates ``tool.unavailable``.

The event has no widget yet (a follow-up). The adapter must dispatch
it without raising and without mounting anything, on the live path
and on replay alike.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cli.chat.event_adapter import EventAdapter
from schemas.events import EventType

if TYPE_CHECKING:
    from collections.abc import Callable

    from schemas.events import Event


class _RecordingApp:
    """App double: every attribute is a recorded call (sync or async)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        calls = self.calls

        async def _record(*args: Any, **kwargs: Any) -> None:
            _ = args, kwargs
            calls.append(name)

        return _record


async def test_event_adapter_ignores_tool_unavailable(
    canonical_event: Callable[[EventType], Event],
) -> None:
    app = _RecordingApp()
    adapter = EventAdapter(app)
    event = canonical_event(EventType.TOOL_UNAVAILABLE)
    assert EventAdapter.handles(EventType.TOOL_UNAVAILABLE)

    await adapter.handle(event)
    await adapter.handle(event)  # replay of the same row is also a no-op

    assert app.calls == []
