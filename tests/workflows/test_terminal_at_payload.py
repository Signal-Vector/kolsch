"""spec-002 Test 5 — ``terminal_at`` on workflow terminal payloads.

Pins the ``terminal_at: datetime`` field addition on the three
terminal payload classes. Verifies:

- Default-factory sets it to a UTC-aware datetime within the last
  second when no explicit value is passed.
- Explicit values pass through unchanged (equal down to the
  microsecond).
- ``model_dump`` + ``model_validate`` round-trips preserve the
  microsecond value.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from workflows.events import (
    WorkflowRunAbortedPayload,
    WorkflowRunCompletedPayload,
    WorkflowRunFailedPayload,
)


def _is_recent_utc(ts: datetime) -> bool:
    """Return True iff ``ts`` is a UTC-aware datetime within the last
    second. Uses a wall-clock delta so tests aren't order-sensitive."""
    if ts.tzinfo is None:
        return False
    now = datetime.now(tz=UTC)
    return timedelta(seconds=0) <= (now - ts) < timedelta(seconds=1)


def test_completed_payload_default_terminal_at_is_recent_utc() -> None:
    """Constructing with no explicit ``terminal_at`` sets a
    fresh UTC-aware timestamp inside the last second."""
    payload = WorkflowRunCompletedPayload()
    assert isinstance(payload.terminal_at, datetime)
    assert payload.terminal_at.tzinfo is UTC
    assert _is_recent_utc(payload.terminal_at)


def test_failed_payload_default_terminal_at_is_recent_utc() -> None:
    payload = WorkflowRunFailedPayload()
    assert isinstance(payload.terminal_at, datetime)
    assert payload.terminal_at.tzinfo is UTC
    assert _is_recent_utc(payload.terminal_at)


def test_aborted_payload_default_terminal_at_is_recent_utc() -> None:
    payload = WorkflowRunAbortedPayload()
    assert isinstance(payload.terminal_at, datetime)
    assert payload.terminal_at.tzinfo is UTC
    assert _is_recent_utc(payload.terminal_at)


@pytest.mark.parametrize(
    "payload_cls",
    [
        WorkflowRunCompletedPayload,
        WorkflowRunFailedPayload,
        WorkflowRunAbortedPayload,
    ],
)
def test_explicit_terminal_at_passes_through(payload_cls: type) -> None:
    """An explicit ``terminal_at`` value overrides the default-factory
    and passes through unchanged. Uses the same fixed datetime for
    each payload class."""
    fixed = datetime(2020, 1, 1, tzinfo=UTC)
    payload = payload_cls(terminal_at=fixed)
    assert payload.terminal_at == fixed


def test_completed_payload_terminal_at_round_trips_to_microsecond() -> None:
    """``model_dump`` -> ``model_validate`` preserves ``terminal_at``
    down to the microsecond — the JSONL round-trip is exact."""
    fixed = datetime(2020, 6, 15, 12, 30, 45, 654_321, tzinfo=UTC)
    payload = WorkflowRunCompletedPayload(terminal_at=fixed)
    dumped = payload.model_dump(mode="json")
    rebuilt = WorkflowRunCompletedPayload.model_validate(dumped)
    assert rebuilt.terminal_at == fixed
