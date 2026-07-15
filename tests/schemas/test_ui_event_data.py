"""spec-002 Test 2 — the ``UIEventData`` payload model.

Pins the dotted-namespace ``kind`` validator, default field values,
JSON round-trip, and forward-compat ``extra="ignore"`` behaviour
inherited from :class:`_DataModel`.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas import UIEventData


def test_valid_kind_constructs_with_defaults() -> None:
    """A dotted 3-component kind + payload constructs cleanly. Every
    default field takes its declared value."""
    ev = UIEventData(kind="kolsch.card.opened", payload={"card_id": "c1"})
    assert ev.kind == "kolsch.card.opened"
    assert ev.schema_version == 1
    assert ev.payload == {"card_id": "c1"}
    assert ev.logical_seq == 0


def test_single_component_kind_rejects_with_dotted_hint() -> None:
    """A non-dotted kind (single component) is a construction-time
    ValidationError. The error message references the dotted
    contract."""
    with pytest.raises(ValidationError) as excinfo:
        UIEventData(kind="oops", payload={})
    assert "dotted" in str(excinfo.value)


def test_two_component_kind_rejects() -> None:
    """Only two components (one dot) still fails — three-minimum."""
    with pytest.raises(ValidationError):
        UIEventData(kind="kolsch.card", payload={})


def test_four_component_kind_accepts() -> None:
    """Depth > 3 is allowed — validator uses ``>= 3`` not ``== 3``.
    Locks in the roadmap invariant that
    ``kolsch.workflow.node.awaiting_approval`` (spec-014) is legal."""
    ev = UIEventData(
        kind="kolsch.workflow.node.awaiting_approval",
        payload={},
    )
    assert ev.kind == "kolsch.workflow.node.awaiting_approval"


def test_json_round_trip_preserves_fields() -> None:
    """``model_dump_json`` -> ``model_validate_json`` reconstructs an
    equivalent instance for a canonical payload."""
    original = UIEventData(kind="kolsch.card.opened", payload={"card_id": "c1"})
    dumped = original.model_dump_json()
    rebuilt = UIEventData.model_validate_json(dumped)
    assert rebuilt == original


def test_extra_fields_are_ignored_on_validate() -> None:
    """``_DataModel`` is now ``extra="ignore"`` — a JSON blob with an
    unknown field validates without raising. Locks the forward-compat
    replay contract from spec-002."""
    payload_json = (
        '{"kind": "kolsch.card.opened", "payload": {}, "unknown_future_field": 42}'
    )
    ev = UIEventData.model_validate_json(payload_json)
    assert ev.kind == "kolsch.card.opened"
    # Extra field is DROPPED (not stored) — that's what "ignore" means.
    assert not hasattr(ev, "unknown_future_field")
