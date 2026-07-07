"""spec-002 Test 4 — ``_DataModel`` base flip + ``logical_seq`` field.

Pins that:

- ``model_config["extra"] == "ignore"`` (was ``"forbid"`` pre-spec-002).
- The ``logical_seq: int = 0`` field is on the base and inherits into
  every subclass.
- Unknown extra fields on a canonical subclass round-trip cleanly
  (forward-compat replay tolerance).
- Explicit ``logical_seq`` values pass through unchanged.
"""

from __future__ import annotations

from uuid import uuid4

from schemas import SessionOpenedData
from schemas.events import _DataModel
from schemas.identifiers import SessionId


def _ses() -> SessionId:
    return SessionId(f"ses_{uuid4()}")


def test_data_model_config_is_extra_ignore() -> None:
    """spec-002 flips the base config; forbidding extras on
    ``_DataModel`` is retired in favour of forward-compat replay."""
    assert _DataModel.model_config["extra"] == "ignore"


def test_data_model_declares_logical_seq_field() -> None:
    """Every payload carries ``logical_seq`` from the base — locks the
    field's presence on the model class rather than any one subclass."""
    assert "logical_seq" in _DataModel.model_fields
    field = _DataModel.model_fields["logical_seq"]
    assert field.annotation is int
    assert field.default == 0


def test_subclass_inherits_logical_seq_default() -> None:
    """A pre-spec-002 subclass (``SessionOpenedData``) inherits
    ``logical_seq`` transparently. Legacy construction (no explicit
    value) picks up default 0; explicit population passes through."""
    default_data = SessionOpenedData(
        session_id=_ses(),
        initial_agent={"id": "ag_x", "name": "x"},
    )
    assert default_data.logical_seq == 0

    populated = SessionOpenedData(
        session_id=_ses(),
        initial_agent={"id": "ag_x", "name": "x"},
        logical_seq=42,
    )
    assert populated.logical_seq == 42


def test_subclass_round_trip_tolerates_unknown_extra_field() -> None:
    """A canonical subclass dumped, decorated with an unknown extra
    field, and re-validated succeeds — this is the forward-compat
    contract spec-002 unlocks. The unknown field is dropped
    (``extra="ignore"`` semantics)."""
    original = SessionOpenedData(
        session_id=_ses(),
        initial_agent={"id": "ag_x", "name": "x"},
    )
    dumped = original.model_dump(mode="json")
    dumped["unknown_future_field"] = "harmless"
    rebuilt = SessionOpenedData.model_validate(dumped)
    assert rebuilt.session_id == original.session_id
    assert rebuilt.initial_agent == original.initial_agent
    assert rebuilt.logical_seq == 0
    assert not hasattr(rebuilt, "unknown_future_field")
