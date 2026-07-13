"""feat-054 Phase 3 — chat notice widget for compaction events.

Drives four canonical payloads through
:class:`cli.chat.event_adapter.EventAdapter` and asserts the exact
notice text + kind mounted at the transcript root:

- ``CONTEXT_COMPACTED`` (success) → info notice with the ``compacted
  N events`` line.
- ``CONTEXT_COMPACTED`` with ``preview=True`` → info notice with the
  ``compaction preview`` line.
- ``CONTEXT_COMPACTION_REVOKED`` → info notice naming the restored
  compaction.
- ``CONTEXT_COMPACTION_FAILED`` → warning notice carrying the error
  message.
- ``CONTEXT_COMPACTION_SKIPPED`` (no_net_savings) → info notice.
- ``CONTEXT_COMPACTION_SKIPPED`` (other reason) → warning notice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from cli.chat.event_adapter import EventAdapter
from schemas import (
    Actor,
    Event,
    EventId,
    EventType,
    SessionId,
    ThreadId,
)


class _RecordingTranscript:
    """Captures every ``mount(widget)`` call so tests can inspect the
    text + kind of the notice mounted by ``EventAdapter``.

    The event adapter mounts through
    ``_locate_transcript().mount(widget)``. Stubbing that call chain
    is cleaner than patching the adapter's private methods.
    """

    def __init__(self) -> None:
        self.mounted: list[Any] = []

    async def mount(self, widget: Any) -> None:
        self.mounted.append(widget)


class _CapturingApp:
    """Mock app returning a recording transcript for the adapter to mount into."""

    def __init__(self) -> None:
        self.transcript = _RecordingTranscript()

    def query_one(self, selector: str, *_args: Any, **_kwargs: Any) -> Any:
        if selector == "#transcript":
            return self.transcript
        raise LookupError(f"unknown selector {selector!r}")

    def _mount_root_notice(self, *_args: Any, **_kwargs: Any) -> None:
        raise AttributeError(
            "app-level shim shouldn't be called — adapter mounts directly"
        )


def _first_mount_text_kind(app: _CapturingApp) -> tuple[str, str]:
    """Return ``(text, kind)`` from the first mounted ``NoticeWidget``."""
    assert app.transcript.mounted, "no widget was mounted"
    widget = app.transcript.mounted[0]
    return widget.text, widget.kind


def _fresh_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _event(
    event_type: EventType,
    data: dict[str, Any],
    *,
    event_id: str | None = None,
) -> Event:
    return Event(
        event_id=EventId(event_id or _fresh_id("ev")),
        parent_event_id=None,
        thread_id=ThreadId(_fresh_id("th")),
        session_id=SessionId(_fresh_id("ses")),
        type=event_type,
        data=data,
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_context_compacted_success_mounts_info_notice() -> None:
    app = _CapturingApp()
    adapter = EventAdapter(app)
    event = _event(
        EventType.CONTEXT_COMPACTED,
        {
            "compacted_event_ids": [f"ev_x_{i:032d}" for i in range(47)],
            "pre_compaction": {},
            "summary": {"schema_version": 1},
            "tokens_before": 12043,
            "tokens_after": 1180,
            "trigger": "auto",
            "preview": False,
        },
    )
    await adapter.handle(event)
    text, kind = _first_mount_text_kind(app)
    assert "compacted 47 events" in text
    assert "12,043" in text and "1,180" in text
    assert "/uncompact to restore" in text
    assert "/why-compact for details" in text
    assert kind == "info"


@pytest.mark.asyncio
async def test_context_compacted_preview_mounts_preview_notice() -> None:
    app = _CapturingApp()
    adapter = EventAdapter(app)
    event = _event(
        EventType.CONTEXT_COMPACTED,
        {
            "compacted_event_ids": [f"ev_x_{i:032d}" for i in range(47)],
            "pre_compaction": {},
            "summary": {"schema_version": 1},
            "tokens_before": 12000,
            "tokens_after": 1000,
            "trigger": "manual_preview",
            "preview": True,
        },
    )
    await adapter.handle(event)
    text, kind = _first_mount_text_kind(app)
    assert "compaction preview" in text
    assert "would compact 47 events" in text
    assert "Not applied" in text
    assert kind == "info"


@pytest.mark.asyncio
async def test_context_compaction_revoked_mounts_info_notice() -> None:
    app = _CapturingApp()
    adapter = EventAdapter(app)
    target_id = "ev_deadbeefcafe0000cafebabe12345678"
    event = _event(
        EventType.CONTEXT_COMPACTION_REVOKED,
        {
            "compaction_event_id": target_id,
            "reason": "user_requested",
        },
    )
    await adapter.handle(event)
    text, kind = _first_mount_text_kind(app)
    assert "restored" in text
    # Notice shows the short-id form of the pointed-at compaction id.
    assert target_id[:12] in text
    assert kind == "info"


@pytest.mark.asyncio
async def test_context_compaction_failed_mounts_warning_notice() -> None:
    app = _CapturingApp()
    adapter = EventAdapter(app)
    event = _event(
        EventType.CONTEXT_COMPACTION_FAILED,
        {
            "error": {"type": "SummariserTimeout", "message": "LLM timed out"},
        },
    )
    await adapter.handle(event)
    text, kind = _first_mount_text_kind(app)
    assert "compaction failed" in text
    assert "LLM timed out" in text
    assert kind == "warning"


@pytest.mark.asyncio
async def test_context_compaction_skipped_info_when_no_net_savings() -> None:
    app = _CapturingApp()
    adapter = EventAdapter(app)
    event = _event(
        EventType.CONTEXT_COMPACTION_SKIPPED,
        {"reason": "no_net_savings", "tokens_before": 100, "tokens_after": 95},
    )
    await adapter.handle(event)
    text, kind = _first_mount_text_kind(app)
    assert "no_net_savings" in text
    assert kind == "info"


@pytest.mark.asyncio
async def test_context_compaction_skipped_warning_on_other_reason() -> None:
    app = _CapturingApp()
    adapter = EventAdapter(app)
    event = _event(
        EventType.CONTEXT_COMPACTION_SKIPPED,
        {"reason": "context_length_unknown"},
    )
    await adapter.handle(event)
    text, kind = _first_mount_text_kind(app)
    assert "context_length_unknown" in text
    assert kind == "warning"
