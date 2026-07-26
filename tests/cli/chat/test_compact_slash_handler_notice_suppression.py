"""bug-098 AC-4 regression guard — `/compact` opaque-notice suppression.

`_handle_compact` at ``src/cli/chat/app.py:2280-2296`` mounts an opaque
three-way notice ("nothing to compact — short session, compaction
disabled, or model has no declared context_length") ONLY when
``manual_compact`` returns ``None``. Post-bug-098 the wedged
``no_context_length`` path returns a ``CONTEXT_COMPACTION_SKIPPED``
event instead of ``None``, so this branch is bypassed and the event
adapter's :meth:`_handle_context_compaction_skipped` renders the
distinct :class:`CompactionBlock` with the remediation hint instead.

This test locks that discriminator in. A future refactor of
``_handle_compact`` that reintroduces an alternate not-None path
(sentinel enum, tuple, etc.) could silently re-open the double-notice
bug where BOTH the opaque three-way string AND the distinct block get
mounted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from cli.chat.app import ChatApp
from schemas import (
    Actor,
    Event,
    EventId,
    EventType,
    SessionId,
    ThreadId,
)


def _skip_event(reason: str) -> Event:
    """Build a ``CONTEXT_COMPACTION_SKIPPED`` event with the given reason."""
    return Event(
        event_id=EventId(f"ev_{uuid4().hex}"),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        parent_event_id=None,
        created_at=datetime.now(UTC),
        type=EventType.CONTEXT_COMPACTION_SKIPPED,
        data={
            "reason": reason,
            "tokens_before": 0,
            "tokens_after": 0,
            "detail": {
                "provider": "opencode",
                "model": "kimi-k2.6",
                "remediation_hint": "Run kolsch refresh opencode …",
            },
        },
        actor=Actor(kind="system"),
    )


def _make_mock_self(*, manual_compact_returns: Any) -> Any:
    """Build a MagicMock ``self`` shaped like ``ChatApp`` for _handle_compact.

    Only mocks the surface `_handle_compact` actually touches — leaves
    the method itself unbound so we exercise the shipping code.
    """
    mock_self = MagicMock()
    mock_self._mount_transcript_notice = AsyncMock()
    mock_self.engine.manual_compact = AsyncMock(return_value=manual_compact_returns)
    mock_self.session.config = MagicMock()
    mock_self.session.thread_id = "th_test"
    mock_self.event_adapter._compaction_widgets = {}
    # ``self.query_one("#transcript", VerticalScroll).mount(widget)`` —
    # let it succeed (async) so the pending-block try block completes
    # cleanly and we reach the manual_compact call.
    transcript = MagicMock()
    transcript.mount = AsyncMock()
    mock_self.query_one.return_value = transcript
    return mock_self


@pytest.mark.asyncio
async def test_skipped_no_context_length_does_not_mount_opaque_notice() -> None:
    """bug-098 AC-4: when manual_compact returns SKIPPED(no_context_length), the
    slash handler must NOT mount the opaque three-way "nothing to compact" notice.
    """
    mock_self = _make_mock_self(
        manual_compact_returns=_skip_event("no_context_length"),
    )

    await ChatApp._handle_compact(mock_self, "")

    # Inspect every call to _mount_transcript_notice; none should be the
    # opaque three-way string.
    calls = mock_self._mount_transcript_notice.call_args_list
    opaque = [c for c in calls if "nothing to compact" in str(c)]
    assert not opaque, (
        f"opaque three-way notice unexpectedly mounted when manual_compact "
        f"returned a CONTEXT_COMPACTION_SKIPPED event; calls={calls!r}"
    )


@pytest.mark.asyncio
async def test_none_return_still_mounts_opaque_notice() -> None:
    """Regression guard — when manual_compact returns None (genuine
    short-session / no-provider), the opaque notice MUST still fire so
    the user gets feedback that ``/compact`` did nothing.

    This complements the AC-4 test: proves the discriminator hasn't
    over-corrected by suppressing the notice in ALL cases.
    """
    mock_self = _make_mock_self(manual_compact_returns=None)

    await ChatApp._handle_compact(mock_self, "")

    calls = mock_self._mount_transcript_notice.call_args_list
    opaque = [c for c in calls if "nothing to compact" in str(c)]
    assert opaque, (
        f"opaque three-way notice must fire when manual_compact returns "
        f"None; calls={calls!r}"
    )
