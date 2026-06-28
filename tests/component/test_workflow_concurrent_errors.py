"""Phase 3.10 Step 0 — error class + event payload field declarations.

Covers ACs 0.3 + 0.4 from the build doc:

- :class:`WorkflowParallelStateConflict` renders the canonical
  single-line YAML-origin form with the three identifying names
  (``node_a``, ``node_b``, ``state_key``).
- :class:`WorkflowInvalidConcurrency` renders with
  (``node``, ``value``).
- Both carry the documented ``caller_message`` and
  ``identifying_names_keys`` ClassVars (R3 #8 + #11).
- The new event payload models (:class:`WorkflowRunStartedPayload`,
  :class:`WorkflowRunCompletedPayload`) round-trip pre-Phase-3.10
  payload dicts unchanged — the new optional fields default to
  ``None`` and unknown extras pass through (``extra="allow"``).
"""

from __future__ import annotations

import pytest

from workflows.errors import (
    WorkflowInvalidConcurrency,
    WorkflowParallelStateConflict,
    WorkflowValidationError,
)
from workflows.events import (
    WorkflowRunCompletedPayload,
    WorkflowRunStartedPayload,
)


# ---------------------------------------------------------------------------
# AC 0.3 — WorkflowParallelStateConflict
# ---------------------------------------------------------------------------


def test_parallel_state_conflict_is_validation_error() -> None:
    """Subclass of :class:`WorkflowValidationError` so the run-start
    gate aggregator picks it up alongside other parse-time errors."""
    assert issubclass(WorkflowParallelStateConflict, WorkflowValidationError)


def test_parallel_state_conflict_identifying_names_keys() -> None:
    assert WorkflowParallelStateConflict.identifying_names_keys == (
        "node_a",
        "node_b",
        "state_key",
    )


def test_parallel_state_conflict_caller_message_non_empty() -> None:
    cm = WorkflowParallelStateConflict.caller_message
    assert isinstance(cm, str) and cm
    # Master spec §16.4 caller message lines.
    assert "parallel" in cm.lower()
    assert "fix" in cm.lower()


def test_parallel_state_conflict_canonical_single_line() -> None:
    err = WorkflowParallelStateConflict(
        file="wf.yaml",
        line=12,
        node_a="alpha",
        node_b="beta",
        state_key="counter",
    )
    rendered = str(err)
    # Canonical YAML-origin format: <ClassName>: <file>:<line> <ids>: <problem>. Fix: <action>.
    assert rendered.startswith("WorkflowParallelStateConflict: wf.yaml:12")
    assert "node_a='alpha'" in rendered
    assert "node_b='beta'" in rendered
    assert "state_key='counter'" in rendered
    assert "Fix: " in rendered
    # Single line.
    assert "\n" not in rendered


def test_parallel_state_conflict_attributes_preserved() -> None:
    err = WorkflowParallelStateConflict(
        file="path/to/wf.yaml",
        line=42,
        node_a="a",
        node_b="b",
        state_key="k",
    )
    assert err.file == "path/to/wf.yaml"
    assert err.line == 42
    assert err.node_a == "a"
    assert err.node_b == "b"
    assert err.state_key == "k"


# ---------------------------------------------------------------------------
# AC 0.3 — WorkflowInvalidConcurrency
# ---------------------------------------------------------------------------


def test_invalid_concurrency_is_validation_error() -> None:
    assert issubclass(WorkflowInvalidConcurrency, WorkflowValidationError)


def test_invalid_concurrency_identifying_names_keys() -> None:
    assert WorkflowInvalidConcurrency.identifying_names_keys == ("node", "value")


def test_invalid_concurrency_caller_message_non_empty() -> None:
    cm = WorkflowInvalidConcurrency.caller_message
    assert isinstance(cm, str) and cm
    assert "concurrency" in cm.lower()


def test_invalid_concurrency_canonical_single_line() -> None:
    err = WorkflowInvalidConcurrency(
        file="wf.yaml",
        line=7,
        node="fan_out",
        value=-1,
    )
    rendered = str(err)
    assert rendered.startswith("WorkflowInvalidConcurrency: wf.yaml:7")
    assert "node='fan_out'" in rendered
    assert "value=-1" in rendered
    assert "Fix: " in rendered
    assert "\n" not in rendered


@pytest.mark.parametrize("value", [-1, 0, -10, 1.5])
def test_invalid_concurrency_accepts_any_offending_value(value: object) -> None:
    """The identifying ``value`` is rendered with ``repr`` so floats,
    ints, and other types all round-trip into the line."""
    err = WorkflowInvalidConcurrency(
        file="wf.yaml", line=1, node="m", value=value
    )
    assert repr(value) in str(err)


# ---------------------------------------------------------------------------
# AC 0.4 — event payload models round-trip pre-Phase-3.10 payloads
# ---------------------------------------------------------------------------


def test_run_started_payload_accepts_legacy_dict() -> None:
    """A pre-Phase-3.10 ``workflow.run.started`` payload (the dict
    emitted at ``src/workflows/executor.py:452``) round-trips cleanly
    through the new typed payload — ``concurrency_mode`` defaults to
    ``None`` and the existing keys flow through ``extra="allow"``."""
    legacy = {
        "workflow_ref": "acme/workflows/hello",
        "inputs": {"name": "world"},
        "trace_id": "tr_abc123",
    }
    payload = WorkflowRunStartedPayload.model_validate(legacy)
    assert payload.concurrency_mode is None
    dumped = payload.model_dump(mode="json")
    assert dumped["workflow_ref"] == "acme/workflows/hello"
    assert dumped["inputs"] == {"name": "world"}
    assert dumped["trace_id"] == "tr_abc123"


def test_run_started_payload_accepts_concurrency_mode_field() -> None:
    payload = WorkflowRunStartedPayload.model_validate(
        {"workflow_ref": "x", "concurrency_mode": "auto"}
    )
    assert payload.concurrency_mode == "auto"

    payload = WorkflowRunStartedPayload.model_validate(
        {"workflow_ref": "x", "concurrency_mode": "sequential"}
    )
    assert payload.concurrency_mode == "sequential"


def test_run_started_payload_rejects_bad_concurrency_mode() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WorkflowRunStartedPayload.model_validate({"concurrency_mode": "foo"})


def test_run_completed_payload_accepts_legacy_dict() -> None:
    """A pre-Phase-3.10 ``workflow.run.completed`` payload (the dict
    emitted at ``src/workflows/executor.py:1466``) round-trips cleanly
    through the new typed payload — ``peak_in_flight`` defaults to
    ``None`` and ``outputs`` flows through ``extra="allow"``."""
    legacy = {"outputs": {"greet": {"stdout": "hi\n", "exit_code": 0}}}
    payload = WorkflowRunCompletedPayload.model_validate(legacy)
    assert payload.peak_in_flight is None
    dumped = payload.model_dump(mode="json")
    assert dumped["outputs"] == {"greet": {"stdout": "hi\n", "exit_code": 0}}


def test_run_completed_payload_accepts_peak_in_flight() -> None:
    payload = WorkflowRunCompletedPayload.model_validate(
        {"outputs": {}, "peak_in_flight": 4}
    )
    assert payload.peak_in_flight == 4


def test_run_completed_payload_peak_in_flight_int_required() -> None:
    """``peak_in_flight`` is typed ``int | None`` — strings reject."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WorkflowRunCompletedPayload.model_validate(
            {"peak_in_flight": "four"}
        )


def test_event_classes_payload_field_unchanged_at_step_0() -> None:
    """AC 0.5 — the event classes' ``payload`` annotation remains
    ``dict[str, Any]`` at Step 0; Step 3 wires the typed payload.
    Asserted by inspecting the existing event classes' field types.
    """
    from workflows.events import (
        WorkflowRunCompletedEvent,
        WorkflowRunStartedEvent,
    )

    started_field = WorkflowRunStartedEvent.model_fields["payload"]
    completed_field = WorkflowRunCompletedEvent.model_fields["payload"]
    # The field annotation should still be the base dict[str, Any] from
    # ``WorkflowEvent`` — no typed-payload override yet.
    assert started_field.annotation is not WorkflowRunStartedPayload
    assert completed_field.annotation is not WorkflowRunCompletedPayload
