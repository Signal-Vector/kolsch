"""feat-054 — chat widget for compaction events (CompactionBlock).

Drives the canonical payloads through
:class:`cli.chat.event_adapter.EventAdapter` and asserts the widget
state + title mounted at the transcript root:

- ``CONTEXT_COMPACTED`` (success) → ``state="success"`` with token
  savings in the title.
- ``CONTEXT_COMPACTED`` with ``preview=True`` → ``state="preview"``.
- ``CONTEXT_COMPACTION_REVOKED`` → ``state="revoked"`` naming the
  restored compaction.
- ``CONTEXT_COMPACTION_FAILED`` → ``state="failure"`` carrying the
  error message.
- ``CONTEXT_COMPACTION_SKIPPED`` → ``state="skipped"`` with the reason.
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


def _first_mount(app: _CapturingApp) -> tuple[str, str, str]:
    """Return ``(state, title, body)`` from the first mounted CompactionBlock."""
    assert app.transcript.mounted, "no widget was mounted"
    widget = app.transcript.mounted[0]
    return widget.state, str(widget.title), widget.body_text


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
    state, title, body = _first_mount(app)
    assert state == "success"
    assert "Compacted 47 events" in title
    assert "12,043" in title and "1,180" in title
    assert "/uncompact to restore" in body
    assert "/why-compact" in body


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
    state, title, body = _first_mount(app)
    assert state == "preview"
    assert "Compaction preview" in title
    assert "would compact 47 events" in title
    assert "not applied" in title.lower() or "not applied" in body.lower()


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
    state, title, _body = _first_mount(app)
    assert state == "revoked"
    assert "restored" in title
    # Widget shows the short-id form of the pointed-at compaction id.
    assert target_id[:12] in title


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
    state, title, body = _first_mount(app)
    assert state == "failure"
    assert "Compaction failed" in title
    assert "LLM timed out" in body or "LLM timed out" in title


@pytest.mark.asyncio
async def test_context_compaction_skipped_info_when_no_net_savings() -> None:
    app = _CapturingApp()
    adapter = EventAdapter(app)
    event = _event(
        EventType.CONTEXT_COMPACTION_SKIPPED,
        {"reason": "no_net_savings", "tokens_before": 100, "tokens_after": 95},
    )
    await adapter.handle(event)
    state, title, body = _first_mount(app)
    assert state == "skipped"
    assert "no_net_savings" in title or "no_net_savings" in body


@pytest.mark.asyncio
async def test_context_compaction_skipped_warning_on_other_reason() -> None:
    app = _CapturingApp()
    adapter = EventAdapter(app)
    event = _event(
        EventType.CONTEXT_COMPACTION_SKIPPED,
        {"reason": "context_length_unknown"},
    )
    await adapter.handle(event)
    state, title, body = _first_mount(app)
    assert state == "skipped"
    assert "context_length_unknown" in title or "context_length_unknown" in body


# ---------------------------------------------------------------------------
# qol-092 — CONTEXT_COMPACTION_SKIPPED(no_context_length) rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_compaction_skipped_no_context_length_renders_detail() -> None:
    """qol-092 AC-2: no_context_length reason renders model spec + hint."""
    app = _CapturingApp()
    adapter = EventAdapter(app)
    event = _event(
        EventType.CONTEXT_COMPACTION_SKIPPED,
        {
            "reason": "no_context_length",
            "tokens_before": 0,
            "tokens_after": 0,
            "detail": {
                "provider": "opencode",
                "model": "kimi-k2.6",
                "remediation_hint": (
                    "Run `kolsch refresh opencode` to backfill "
                    "`context_length` from the connected metadata source."
                ),
            },
        },
    )
    await adapter.handle(event)
    state, title, body = _first_mount(app)
    assert state == "skipped"
    # Title still names the reason (same shape as other skip reasons).
    assert "no_context_length" in title
    # Body carries the model spec and the remediation hint (multi-line).
    assert "opencode/kimi-k2.6" in body
    assert "kolsch refresh opencode" in body


@pytest.mark.asyncio
async def test_context_compaction_skipped_no_net_savings_body_unchanged() -> None:
    """qol-092 AC-2 regression guard: no_net_savings body stays one-line.

    The old ``transition_to_skipped(reason=...)`` produced body
    ``"reason: no_net_savings"``. qol-092's ``detail`` branch must not
    fire when ``detail`` is absent — this test pins the byte-shape of
    the pre-qol path.
    """
    app = _CapturingApp()
    adapter = EventAdapter(app)
    event = _event(
        EventType.CONTEXT_COMPACTION_SKIPPED,
        {"reason": "no_net_savings", "tokens_before": 100, "tokens_after": 95},
    )
    await adapter.handle(event)
    _state, _title, body = _first_mount(app)
    assert body == "reason: no_net_savings"
