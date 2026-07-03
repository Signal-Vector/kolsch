"""Phase 3.8 Step 0 — fork prereq plumbing tests.

Covers ACs 0.1-0.5 + 0.10 of ``process/phase3.8-fork-build.md``:

- 0.1 ``ForkedFrom`` — frozen + ``extra="forbid"`` + carries
  ``run_id`` / ``fork_event_id``.
- 0.2 ``WorkflowRun.forked_from`` — backward-compat default ``None``.
- 0.3 ``WorkflowRunStore.create(forked_from=)`` — round-trips through
  ``meta.json`` (serialised as a NESTED dict), survives alongside
  ``parent`` (Phase 3.7 pointer).
- 0.4 ``WorkflowRunNotFound`` — exists; renders correctly through
  ``render_exception``.
- 0.5 ``WorkflowEventKind.RUN_FORKED_FROM`` + event class round-trip.
- 0.9 No fork logic yet (grep ``start_from_fork`` / ``_cmd_fork``
  in src/ returns zero matches).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from cli._format import render_exception
from schemas.entities import ForkedFrom, ParentRunPointer
from schemas.identifiers import EventId
from workflows.errors import WorkflowRunNotFound, WorkflowRuntimeError
from workflows.events import (
    WorkflowEventKind,
    WorkflowRunForkedFromEvent,
    WorkflowRunForkedFromPayload,
)
from workflows.ids import InvocationId, TraceId, WorkflowRunId
from workflows.run_store import WorkflowRunStore

_PARENT_RUN_ID = WorkflowRunId("wrk_3b9e7e84-9a1d-4ce6-b6c1-2a4f8a91d101")
_PARENT_INV_ID = InvocationId("inv_2c8c01fa-4b7a-4c0c-b5fb-c19c1c0d3401")
_FORK_EVENT_ID = EventId("ev_8f31c4d2-0001-4000-8000-000000000001")
_TRACE_ID = TraceId("tr_5a7e1ef0-0000-4000-9000-000000000001")


# ---------------------------------------------------------------------------
# AC 0.1 — ForkedFrom value object
# ---------------------------------------------------------------------------


class TestForkedFrom:
    def test_frozen_and_carries_two_fields(self) -> None:
        ptr = ForkedFrom(run_id=_PARENT_RUN_ID, fork_event_id=_FORK_EVENT_ID)
        assert ptr.run_id == _PARENT_RUN_ID
        assert ptr.fork_event_id == _FORK_EVENT_ID
        with pytest.raises((ValidationError, TypeError)):
            # frozen=True — attribute mutation must raise.
            ptr.run_id = WorkflowRunId(  # type: ignore[misc]
                "wrk_99999999-9999-4999-9999-999999999999"
            )

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ForkedFrom(
                run_id=_PARENT_RUN_ID,
                fork_event_id=_FORK_EVENT_ID,
                bogus="nope",  # type: ignore[call-arg]
            )

    def test_mirrors_parent_run_pointer_shape(self) -> None:
        # The three pointer classes (FromWorkflowRun, ParentRunPointer,
        # ForkedFrom) are intentionally symmetric so cross-JSONL
        # forensics tooling can switch over them uniformly. Smoke-test
        # the parallel by constructing both alongside.
        forked = ForkedFrom(
            run_id=_PARENT_RUN_ID, fork_event_id=_FORK_EVENT_ID
        )
        parent = ParentRunPointer(
            workflow_run_id=_PARENT_RUN_ID, invocation_id=_PARENT_INV_ID
        )
        assert forked.run_id == parent.workflow_run_id


# ---------------------------------------------------------------------------
# AC 0.2 / 0.3 — WorkflowRun.forked_from default + store round-trip
# ---------------------------------------------------------------------------


class TestForkedFromRoundTrip:
    def test_default_forked_from_is_none(self, tmp_path: Path) -> None:
        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/x", inputs={})
        assert run.forked_from is None
        reread = store.read(run.run_id)
        assert reread.forked_from is None

    def test_forked_from_round_trips_as_nested_dict(
        self, tmp_path: Path
    ) -> None:
        forked = ForkedFrom(
            run_id=_PARENT_RUN_ID, fork_event_id=_FORK_EVENT_ID
        )
        store = WorkflowRunStore(tmp_path)
        run = store.create(
            workflow_ref="demo/workflows/child",
            inputs={"k": 1},
            forked_from=forked,
        )
        meta_path = tmp_path / "runs" / str(run.run_id) / "meta.json"
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        assert isinstance(raw["forked_from"], dict)
        assert raw["forked_from"]["run_id"] == str(_PARENT_RUN_ID)
        assert raw["forked_from"]["fork_event_id"] == str(_FORK_EVENT_ID)
        # And ``read`` reconstructs the typed pointer.
        reread = store.read(run.run_id)
        assert reread.forked_from == forked

    def test_parent_and_forked_from_coexist(self, tmp_path: Path) -> None:
        # AC 0.3 reviewer note — Phase 3.7's ``parent`` and Phase 3.8's
        # ``forked_from`` are independent fields. A forked sub-workflow
        # run carries BOTH; meta.json must round-trip both cleanly.
        parent_ptr = ParentRunPointer(
            workflow_run_id=_PARENT_RUN_ID, invocation_id=_PARENT_INV_ID
        )
        forked = ForkedFrom(
            run_id=_PARENT_RUN_ID, fork_event_id=_FORK_EVENT_ID
        )
        store = WorkflowRunStore(tmp_path)
        run = store.create(
            workflow_ref="demo/workflows/child",
            inputs={},
            parent=parent_ptr,
            forked_from=forked,
        )
        meta_path = tmp_path / "runs" / str(run.run_id) / "meta.json"
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        assert isinstance(raw["parent"], dict)
        assert isinstance(raw["forked_from"], dict)
        reread = store.read(run.run_id)
        assert reread.parent == parent_ptr
        assert reread.forked_from == forked

    def test_forked_from_survives_update_status(
        self, tmp_path: Path
    ) -> None:
        from workflows.run import WorkflowRunStatus

        forked = ForkedFrom(
            run_id=_PARENT_RUN_ID, fork_event_id=_FORK_EVENT_ID
        )
        store = WorkflowRunStore(tmp_path)
        run = store.create(
            workflow_ref="demo/workflows/child",
            inputs={},
            forked_from=forked,
        )
        store.update_status(run.run_id, WorkflowRunStatus.WAITING_FOR_INPUT)
        reread = store.read(run.run_id)
        assert reread.forked_from == forked


# ---------------------------------------------------------------------------
# AC 0.4 — WorkflowRunNotFound exists + renders correctly
# ---------------------------------------------------------------------------


class TestWorkflowRunNotFound:
    def test_subclass_of_runtime_error(self) -> None:
        assert issubclass(WorkflowRunNotFound, WorkflowRuntimeError)

    def test_carries_run_id_in_identifying_names(self) -> None:
        exc = WorkflowRunNotFound(run_id="wrk_missing")
        assert exc.run_id == "wrk_missing"
        assert "wrk_missing" in str(exc)
        # The class-level R3 #11 keys list governs render_exception.
        assert "run_id" in WorkflowRunNotFound.identifying_names_keys

    def test_renders_through_render_exception(self) -> None:
        exc = WorkflowRunNotFound(run_id="wrk_missing")
        envelope = render_exception(exc)
        assert envelope["class"] == "WorkflowRunNotFound"
        assert envelope["identifying_names"] == {"run_id": "wrk_missing"}
        assert envelope["caller_message"]  # non-empty per R3 #8


# ---------------------------------------------------------------------------
# AC 0.5 — RUN_FORKED_FROM event kind + envelope round-trip
# ---------------------------------------------------------------------------


class TestRunForkedFromEvent:
    def test_enum_value(self) -> None:
        assert (
            WorkflowEventKind.RUN_FORKED_FROM
            == "workflow.run.forked_from"
        )

    def test_event_round_trip_via_model_dump(self) -> None:
        evt = WorkflowRunForkedFromEvent(
            workflow_run_id=_PARENT_RUN_ID,
            trace_id=_TRACE_ID,
            parent_event_id=None,  # FIRST event of the forked JSONL
            payload=WorkflowRunForkedFromPayload(
                forked_from_run_id=str(_PARENT_RUN_ID),
                fork_event_id=str(_FORK_EVENT_ID),
                input_overrides={"value": "urgent"},
            ),
        )
        dumped = evt.model_dump(mode="json")
        assert dumped["kind"] == "workflow.run.forked_from"
        assert dumped["parent_event_id"] is None
        assert (
            dumped["payload"]["forked_from_run_id"] == str(_PARENT_RUN_ID)
        )
        assert dumped["payload"]["fork_event_id"] == str(_FORK_EVENT_ID)
        assert dumped["payload"]["input_overrides"] == {"value": "urgent"}
        reloaded = WorkflowRunForkedFromEvent.model_validate(dumped)
        assert reloaded.payload.fork_event_id == str(_FORK_EVENT_ID)
        assert reloaded.payload.input_overrides == {"value": "urgent"}

    def test_input_overrides_default_empty(self) -> None:
        payload = WorkflowRunForkedFromPayload(
            forked_from_run_id="wrk_parent",
            fork_event_id="ev_x",
        )
        assert payload.input_overrides == {}

    def test_payload_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            WorkflowRunForkedFromPayload(
                forked_from_run_id="wrk_parent",
                fork_event_id="ev_x",
                rogue="nope",  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# Step ownership regression gates
#
# Steps 0/1/2 have all shipped their respective surfaces. These tests
# now guard against silent regressions that delete those surfaces —
# a future patch that removes ``start_from_fork`` or ``_cmd_fork``
# silently breaks fork without tripping any user-level test, so the
# greps are kept here as the structural fence.
# ---------------------------------------------------------------------------


_SRC = Path(__file__).resolve().parents[2] / "src"


def _grep(pattern: str, *paths: Path) -> int:
    """Return the count of matching lines across ``paths`` via grep -c.

    Subprocess + grep is the right tool here: we are explicitly
    asserting on STATIC source-file content as it lands on disk,
    not on runtime import behaviour.
    """
    result = subprocess.run(
        ["grep", "-rn", pattern, *(str(p) for p in paths)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 1:  # no matches
        return 0
    return len(
        [line for line in result.stdout.splitlines() if line.strip()]
    )


class TestStepOwnershipGates:
    def test_executor_ships_start_from_fork(self) -> None:
        # Step 1 ships ``start_from_fork``. Gate against silent
        # regression that deletes the method.
        assert (
            _grep("start_from_fork", _SRC / "workflows" / "executor.py")
            > 0
        )

    def test_cli_ships_cmd_fork(self) -> None:
        # Step 2 ships ``_cmd_fork``. Gate against silent regression
        # that deletes the handler.
        assert _grep("_cmd_fork", _SRC / "cli" / "workflows.py") > 0
