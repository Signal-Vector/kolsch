"""Unit tests for the compaction event types and payload models — feat-054 Phase 1.

Pins the extended `ContextCompactedData` shape and the three new
sibling event types (`CONTEXT_COMPACTION_REVOKED`,
`CONTEXT_COMPACTION_FAILED`, `CONTEXT_COMPACTION_SKIPPED`). Pydantic
round-tripping is the load-bearing contract — Assembly and the CLI
event adapter read these payloads back off the JSONL log.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from schemas.error import Error
from schemas.events import (
    ContextCompactedData,
    ContextCompactionFailedData,
    ContextCompactionRevokedData,
    ContextCompactionSkippedData,
    EventType,
)


def _evt() -> str:
    """Return a valid EventId string (`ev_<uuid>`)."""
    return f"ev_{uuid4()}"


# ---------------------------------------------------------------------------
# EventType enum additions
# ---------------------------------------------------------------------------


def test_event_type_context_compaction_revoked_present() -> None:
    assert EventType.CONTEXT_COMPACTION_REVOKED.value == "context.compaction_revoked"


def test_event_type_context_compaction_failed_present() -> None:
    assert EventType.CONTEXT_COMPACTION_FAILED.value == "context.compaction_failed"


def test_event_type_context_compaction_skipped_present() -> None:
    assert EventType.CONTEXT_COMPACTION_SKIPPED.value == "context.compaction_skipped"


def test_original_context_compacted_still_present() -> None:
    # Regression: extending the enum must not shift the pre-existing
    # value.
    assert EventType.CONTEXT_COMPACTED.value == "context.compacted"


# ---------------------------------------------------------------------------
# ContextCompactedData — new fields
# ---------------------------------------------------------------------------


def _minimal_compacted_kwargs() -> dict[str, object]:
    return {
        "compacted_event_ids": [_evt(), _evt()],
        "pre_compaction": {"messages": []},
        "summary": {"schema_version": 1, "refs": []},
        "tokens_before": 12_043,
        "tokens_after": 1_180,
    }


def test_context_compacted_data_defaults_are_backwards_compatible() -> None:
    # Existing emitters that don't set the new fields still validate.
    data = ContextCompactedData(**_minimal_compacted_kwargs())
    assert data.revoked_by is None
    assert data.preview is False
    assert data.trigger == "auto"


def test_context_compacted_data_accepts_new_fields() -> None:
    revoked_by = _evt()
    data = ContextCompactedData(
        **_minimal_compacted_kwargs(),
        revoked_by=revoked_by,
        preview=True,
        trigger="manual_preview",
    )
    assert data.revoked_by == revoked_by
    assert data.preview is True
    assert data.trigger == "manual_preview"


def test_context_compacted_data_accepts_truncation_triggers() -> None:
    # bug-113 — mechanical-truncation fallback stamps a `*_truncation`
    # trigger on BOTH the top-level field and nested summary.trigger.
    for value in ("auto_truncation", "manual_truncation"):
        data = ContextCompactedData(
            **_minimal_compacted_kwargs(),
            trigger=value,
        )
        assert data.trigger == value
        # Round-trips through JSON (the load-bearing on-disk contract).
        restored = ContextCompactedData.model_validate_json(data.model_dump_json())
        assert restored.trigger == value


def test_context_compacted_data_rejects_invalid_trigger_literal() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ContextCompactedData(
            **_minimal_compacted_kwargs(),
            trigger="whatever",
        )


def test_context_compacted_data_round_trips_through_dict() -> None:
    original = ContextCompactedData(
        **_minimal_compacted_kwargs(),
        revoked_by=_evt(),
        preview=True,
        trigger="manual",
    )
    dumped = original.model_dump()
    restored = ContextCompactedData.model_validate(dumped)
    assert restored == original


def test_context_compacted_data_round_trips_json() -> None:
    original = ContextCompactedData(**_minimal_compacted_kwargs())
    json_str = original.model_dump_json()
    restored = ContextCompactedData.model_validate_json(json_str)
    assert restored == original


# ---------------------------------------------------------------------------
# The three new payload classes
# ---------------------------------------------------------------------------


def test_context_compaction_revoked_data_round_trip() -> None:
    data = ContextCompactionRevokedData(compaction_event_id=_evt())
    assert data.reason == "user_requested"
    restored = ContextCompactionRevokedData.model_validate(data.model_dump())
    assert restored == data


def test_context_compaction_revoked_accepts_custom_reason() -> None:
    data = ContextCompactionRevokedData(
        compaction_event_id=_evt(),
        reason="test_reason",
    )
    assert data.reason == "test_reason"


def test_context_compaction_failed_data_round_trip() -> None:
    err = Error(
        type="context.summarizer_failed",
        message="LLM refused",
        where="providers.compaction.reference_summary",
        detail={},
    )
    data = ContextCompactionFailedData(
        error=err,
        span_first_event_id=_evt(),
        span_last_event_id=_evt(),
    )
    restored = ContextCompactionFailedData.model_validate(data.model_dump())
    assert restored == data


def test_context_compaction_failed_span_fields_optional() -> None:
    err = Error(
        type="context.summarizer_failed",
        message="x",
        where="providers.compaction",
        detail={},
    )
    data = ContextCompactionFailedData(error=err)
    assert data.span_first_event_id is None
    assert data.span_last_event_id is None


def test_context_compaction_skipped_data_round_trip() -> None:
    data = ContextCompactionSkippedData(
        reason="no_net_savings",
        tokens_before=10_000,
        tokens_after=9_500,
    )
    restored = ContextCompactionSkippedData.model_validate(data.model_dump())
    assert restored == data


def test_context_compaction_skipped_defaults_tokens_to_zero() -> None:
    data = ContextCompactionSkippedData(reason="unknown_context_length")
    assert data.tokens_before == 0
    assert data.tokens_after == 0
