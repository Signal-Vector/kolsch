"""Phase 3.8 Step 1 — fork executor entry-point tests.

Covers Step 1 ACs 1.1-1.12 of ``process/phase3.8-fork-build.md``:

- 1.1 ``find_event_by_id`` shape (prefix + target, or None).
- 1.2 ``start_from_fork`` reads parent events + truncates at the fork
  point.
- 1.3 ``WorkflowRunNotFound`` raised when parent's events.jsonl
  (or meta) is missing.
- 1.4 ``WorkflowRuntimeError`` raised when fork_event_id is absent
  from the parent's JSONL; message guides the user.
- 1.5 Replay via existing ``replay_into`` reuses Phase 3.4/3.5/3.7
  handlers — verified by the fork-at-completed pipeline restoring
  upstream captured outputs.
- 1.6 ``--input`` overrides correctly applied:
  * ``workflow.node.completed`` → patches the fork-point node's
    captured output keys; the merge ADDS new keys and REPLACES
    existing ones (untouched keys survive).
  * ``workflow.run.started`` → patches workflow-level inputs.
  * other fork-event kinds + overrides → raises.
- 1.7 ``workflow.run.forked_from`` is the FIRST event of the forked
  JSONL; ``parent_event_id`` is None.
- 1.8 trace_id inherited from parent unchanged on every fork event.
- 1.9 ``_continue_worklist`` re-enters cleanly; only nodes downstream
  of the fork point fire (verified by counting node.started events
  in the FORK's JSONL — replayed nodes do not re-emit).
- 1.10 PID sidecar atomically written.
- 1.11 NO CLI wiring yet (gate test in
  :mod:`tests.component.test_workflow_fork_prereq` covers this).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest

from assets.catalog import AssetCatalog
from schemas import EventId
from schemas.entities import ForkedFrom, ParentRunPointer
from workflows._replay import find_event_by_id
from workflows.builtins import ComputeRunner
from workflows.context import NodeContext
from workflows.errors import WorkflowRunNotFound, WorkflowRuntimeError
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.ids import InvocationId, WorkflowRunId
from workflows.parser import Workflow
from workflows.run import WorkflowRun, WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

# ---------------------------------------------------------------------------
# Pipeline fixture — A→B→C using kolsch/nodes/compute.
#
# Compute produces ``{value: <rendered>}``; downstream nodes consume
# upstream's value via edge binds. Using only built-in node types keeps
# the Phase 3.0 validator happy (custom node types would fail the
# catalog-import check; the goal here is to exercise FORK behaviour,
# not authored-node mechanics).
# ---------------------------------------------------------------------------


def _make_pipeline_workflow() -> Workflow:
    """A → B → C: A=10, B doubles A, C echoes B."""
    return Workflow.model_validate(
        {
            "nodes": {
                "A": {"type": "kolsch/nodes/compute", "value": 10},
                "B": {
                    "type": "kolsch/nodes/compute",
                    "value": "{{ inputs.upstream * 2 }}",
                },
                "C": {
                    "type": "kolsch/nodes/compute",
                    "value": "{{ inputs.v }}",
                },
            },
            "graph": {
                "edges": [
                    {
                        "from": "A",
                        "to": "B",
                        "binds": {"upstream": "{{ nodes.A.value }}"},
                    },
                    {
                        "from": "B",
                        "to": "C",
                        "binds": {"v": "{{ nodes.B.value }}"},
                    },
                ]
            },
        }
    )


def _make_registry() -> RunnerRegistry:
    reg = RunnerRegistry()
    reg.register("kolsch/nodes/compute", ComputeRunner())
    return reg


async def _run_pipeline_to_completion(
    tmp_path: Path,
) -> tuple[WorkflowRunStore, WorkflowRun, Workflow]:
    """Run A→B→C end-to-end and return everything needed for fork tests."""
    wf = _make_pipeline_workflow()
    store = WorkflowRunStore(tmp_path)
    parent_run = store.create(workflow_ref="demo/workflows/pipeline", inputs={})
    ex = IterativeExecutor(
        workflow=wf,
        run=parent_run,
        store=store,
        registry=_make_registry(),
        catalog=AssetCatalog(),
    )
    status = await ex.run()
    assert status == WorkflowRunStatus.SUCCEEDED
    # Sanity — parent's three compute nodes all fired with expected values.
    assert ex._captured == {
        "A": {"value": 10},
        "B": {"value": 20},
        "C": {"value": 20},
    }
    # Re-read so we observe the post-run terminal status.
    parent_run = store.read(parent_run.run_id)
    return store, parent_run, wf


def _events(store: WorkflowRunStore, run_id: WorkflowRunId) -> list[dict[str, Any]]:
    return list(store.read_events(run_id))


def _completion_event_id_for(
    events: Iterable[dict[str, Any]], node_name: str
) -> EventId:
    for event in events:
        if event.get("kind") == "workflow.node.completed" and (
            event.get("node_name") == node_name
            or event.get("payload", {}).get("node_name") == node_name
        ):
            return EventId(event["event_id"])
    msg = f"no workflow.node.completed event for node {node_name!r}"
    raise AssertionError(msg)


def _started_event_id(events: Iterable[dict[str, Any]]) -> EventId:
    for event in events:
        if event.get("kind") == "workflow.run.started":
            return EventId(event["event_id"])
    msg = "no workflow.run.started event"
    raise AssertionError(msg)


def _node_started_names(events: Iterable[dict[str, Any]]) -> list[str]:
    """Return the node names that emitted a node.started event.

    A node that was REPLAYED (not re-fired) on a forked run will NOT
    appear here — replay only mutates the executor's private state; it
    does not write back to the forked JSONL.
    """
    out: list[str] = []
    for event in events:
        if event.get("kind") != "workflow.node.started":
            continue
        name = event.get("node_name") or event.get("payload", {}).get("node_name")
        if name is not None:
            out.append(str(name))
    return out


# ---------------------------------------------------------------------------
# AC 1.1 — find_event_by_id
# ---------------------------------------------------------------------------


class TestFindEventById:
    def test_returns_prefix_and_target(self) -> None:
        events = [
            {"event_id": "ev_1", "kind": "a"},
            {"event_id": "ev_2", "kind": "b"},
            {"event_id": "ev_3", "kind": "c"},
        ]
        result = find_event_by_id(events, "ev_2")
        assert result is not None
        prefix, target = result
        assert [e["event_id"] for e in prefix] == ["ev_1", "ev_2"]
        assert target == events[1]

    def test_returns_none_when_missing(self) -> None:
        events = [{"event_id": "ev_1", "kind": "a"}]
        assert find_event_by_id(events, "ev_missing") is None

    def test_empty_input_returns_none(self) -> None:
        assert find_event_by_id([], "ev_1") is None

    def test_first_event_is_included(self) -> None:
        events = [{"event_id": "ev_only", "kind": "a"}]
        result = find_event_by_id(events, "ev_only")
        assert result is not None
        prefix, target = result
        assert prefix == events
        assert target == events[0]


# ---------------------------------------------------------------------------
# AC 1.2, 1.5, 1.7, 1.8, 1.9, 1.10 — fork at workflow.node.completed
# ---------------------------------------------------------------------------


class TestForkAtCompleted:
    async def test_only_downstream_nodes_fire_with_replayed_state(
        self, tmp_path: Path
    ) -> None:
        store, parent_run, wf = await _run_pipeline_to_completion(tmp_path)
        parent_events = _events(store, parent_run.run_id)
        b_completed_id = _completion_event_id_for(parent_events, "B")

        forked_run = store.create(
            workflow_ref=parent_run.workflow_ref,
            inputs=dict(parent_run.inputs),
            trace_id=parent_run.trace_id,
            forked_from=ForkedFrom(
                run_id=parent_run.run_id,
                fork_event_id=b_completed_id,
            ),
        )
        ex = IterativeExecutor(
            workflow=wf,
            run=forked_run,
            store=store,
            registry=_make_registry(),
            catalog=AssetCatalog(),
        )
        status = await ex.start_from_fork(
            fork_event_id=b_completed_id, input_overrides={}
        )
        assert status == WorkflowRunStatus.SUCCEEDED

        fork_events = _events(store, forked_run.run_id)

        # AC 1.9 — only C fires in the fork; A and B are replayed.
        assert _node_started_names(fork_events) == ["C"]

        # AC 1.5 — replay correctly seeded ``_captured`` from the
        # parent's pre-fork completions; C produced ``value: 20``
        # which is B's parent output (= A.value * 2 = 10 * 2).
        c_completion = next(
            e
            for e in fork_events
            if e.get("kind") == "workflow.node.completed"
            and (
                e.get("node_name") == "C"
                or e.get("payload", {}).get("node_name") == "C"
            )
        )
        assert c_completion["payload"]["output"] == {"value": 20}

        # AC 1.7 — workflow.run.forked_from is the FIRST event;
        # parent_event_id is None.
        assert fork_events[0]["kind"] == "workflow.run.forked_from"
        assert fork_events[0]["parent_event_id"] is None
        assert fork_events[0]["payload"]["forked_from_run_id"] == str(parent_run.run_id)
        assert fork_events[0]["payload"]["fork_event_id"] == b_completed_id
        # And no workflow.run.started ahead of it (forked runs inherit
        # the parent's started baseline via replay; they do NOT emit a
        # fresh started event — same discipline as Phase 3.4 resume).
        kinds = [e["kind"] for e in fork_events]
        assert "workflow.run.started" not in kinds

        # AC 1.8 — trace_id inherited; every event on the forked
        # JSONL carries the parent's trace_id.
        parent_trace = str(parent_run.trace_id)
        for event in fork_events:
            assert event["trace_id"] == parent_trace

        # AC 1.10 — PID sidecar holds THIS process's PID, atomically
        # written (no leftover .tmp).
        pid_path = store.directory(forked_run.run_id).pid_path
        assert pid_path.read_text(encoding="utf-8").strip() == str(os.getpid())
        assert not pid_path.with_suffix(".tmp").exists()

    async def test_fork_at_a_completed_runs_only_b_and_c(self, tmp_path: Path) -> None:
        # Reviewer-friendly variant: fork EARLIER in the pipeline; B
        # and C must both fire in the fork using A's replayed output.
        store, parent_run, wf = await _run_pipeline_to_completion(tmp_path)
        a_completed_id = _completion_event_id_for(
            _events(store, parent_run.run_id), "A"
        )

        forked_run = store.create(
            workflow_ref=parent_run.workflow_ref,
            inputs={},
            trace_id=parent_run.trace_id,
            forked_from=ForkedFrom(
                run_id=parent_run.run_id,
                fork_event_id=a_completed_id,
            ),
        )
        ex = IterativeExecutor(
            workflow=wf,
            run=forked_run,
            store=store,
            registry=_make_registry(),
            catalog=AssetCatalog(),
        )
        status = await ex.start_from_fork(
            fork_event_id=a_completed_id, input_overrides={}
        )
        assert status == WorkflowRunStatus.SUCCEEDED

        fork_events = _events(store, forked_run.run_id)
        assert _node_started_names(fork_events) == ["B", "C"]

        b_out = next(
            e["payload"]["output"]
            for e in fork_events
            if e.get("kind") == "workflow.node.completed"
            and (
                e.get("node_name") == "B"
                or e.get("payload", {}).get("node_name") == "B"
            )
        )
        c_out = next(
            e["payload"]["output"]
            for e in fork_events
            if e.get("kind") == "workflow.node.completed"
            and (
                e.get("node_name") == "C"
                or e.get("payload", {}).get("node_name") == "C"
            )
        )
        # B saw A's replayed value (10) and doubled it; C saw B's
        # fresh output (20).
        assert b_out == {"value": 20}
        assert c_out == {"value": 20}


# ---------------------------------------------------------------------------
# AC 1.6 — input overrides
# ---------------------------------------------------------------------------


class TestForkInputOverrides:
    async def test_override_replaces_node_output_key_and_downstream_sees_it(
        self, tmp_path: Path
    ) -> None:
        # B's parent output is {value: 20}. Override value=99 must:
        #  - mutate _captured[B] to {value: 99}
        #  - propagate to C, which renders nodes.B.value → 99
        store, parent_run, wf = await _run_pipeline_to_completion(tmp_path)
        b_completed_id = _completion_event_id_for(
            _events(store, parent_run.run_id), "B"
        )

        forked_run = store.create(
            workflow_ref=parent_run.workflow_ref,
            inputs={},
            trace_id=parent_run.trace_id,
            forked_from=ForkedFrom(
                run_id=parent_run.run_id,
                fork_event_id=b_completed_id,
            ),
        )
        ex = IterativeExecutor(
            workflow=wf,
            run=forked_run,
            store=store,
            registry=_make_registry(),
            catalog=AssetCatalog(),
        )
        status = await ex.start_from_fork(
            fork_event_id=b_completed_id,
            input_overrides={"value": 99},
        )
        assert status == WorkflowRunStatus.SUCCEEDED
        # C captured the OVERRIDDEN value, not the parent's 20.
        assert ex._captured["C"] == {"value": 99}
        # And B's captured dict reflects the override.
        assert ex._captured["B"] == {"value": 99}

        # The forked JSONL records the overrides in the
        # workflow.run.forked_from payload for forensics.
        fork_events = _events(store, forked_run.run_id)
        assert fork_events[0]["payload"]["input_overrides"] == {"value": 99}

    async def test_override_merge_adds_new_keys_preserves_untouched(
        self, tmp_path: Path
    ) -> None:
        # AC 1.6 reviewer note — the merge ADDS new keys onto the
        # captured-output dict without dropping any. B's parent output
        # is {value: 20}. Override {extra: "patched"} (new key only;
        # ``value`` not in the override) must:
        #  - leave ``value=20`` intact
        #  - add ``extra="patched"``
        # Downstream C reads ``v: nodes.B.value`` → 20 (unchanged).
        store, parent_run, wf = await _run_pipeline_to_completion(tmp_path)
        b_completed_id = _completion_event_id_for(
            _events(store, parent_run.run_id), "B"
        )

        forked_run = store.create(
            workflow_ref=parent_run.workflow_ref,
            inputs={},
            trace_id=parent_run.trace_id,
            forked_from=ForkedFrom(
                run_id=parent_run.run_id,
                fork_event_id=b_completed_id,
            ),
        )
        ex = IterativeExecutor(
            workflow=wf,
            run=forked_run,
            store=store,
            registry=_make_registry(),
            catalog=AssetCatalog(),
        )
        status = await ex.start_from_fork(
            fork_event_id=b_completed_id,
            input_overrides={"extra": "patched"},
        )
        assert status == WorkflowRunStatus.SUCCEEDED
        # Both keys present — ``value`` survived, ``extra`` was added.
        assert ex._captured["B"] == {
            "value": 20,
            "extra": "patched",
        }
        # C still sees the untouched value.
        assert ex._captured["C"] == {"value": 20}

    async def test_override_at_started_event_patches_workflow_inputs(
        self, tmp_path: Path
    ) -> None:
        # Fork at workflow.run.started with an override: the new value
        # flows into both ``_run.inputs`` and ``_state`` so a
        # downstream node consuming ``{{ inputs.X }}`` sees the patched
        # value on the fork.
        wf = Workflow.model_validate(
            {
                "nodes": {
                    "probe": {
                        "type": "kolsch/nodes/compute",
                        "value": "{{ inputs.seed }}",
                    },
                },
            }
        )
        store = WorkflowRunStore(tmp_path)
        parent_run = store.create(
            workflow_ref="demo/workflows/state", inputs={"seed": "parent"}
        )
        ex = IterativeExecutor(
            workflow=wf,
            run=parent_run,
            store=store,
            registry=_make_registry(),
            catalog=AssetCatalog(),
        )
        await ex.run()
        assert ex._captured == {"probe": {"value": "parent"}}

        started_id = _started_event_id(_events(store, parent_run.run_id))
        forked_run = store.create(
            workflow_ref=parent_run.workflow_ref,
            inputs=dict(parent_run.inputs),
            trace_id=parent_run.trace_id,
            forked_from=ForkedFrom(run_id=parent_run.run_id, fork_event_id=started_id),
        )
        fork_ex = IterativeExecutor(
            workflow=wf,
            run=forked_run,
            store=store,
            registry=_make_registry(),
            catalog=AssetCatalog(),
        )
        status = await fork_ex.start_from_fork(
            fork_event_id=started_id,
            input_overrides={"seed": "forked"},
        )
        assert status == WorkflowRunStatus.SUCCEEDED
        # The probe saw the OVERRIDDEN state on the fork.
        assert fork_ex._captured == {"probe": {"value": "forked"}}

    async def test_override_at_started_re_runs_all_nodes(self, tmp_path: Path) -> None:
        # AC for Phase Test 3 — fork at started re-runs everything;
        # no replayed completions means every node fires.
        store, parent_run, wf = await _run_pipeline_to_completion(tmp_path)
        started_id = _started_event_id(_events(store, parent_run.run_id))

        forked_run = store.create(
            workflow_ref=parent_run.workflow_ref,
            inputs={},
            trace_id=parent_run.trace_id,
            forked_from=ForkedFrom(run_id=parent_run.run_id, fork_event_id=started_id),
        )
        ex = IterativeExecutor(
            workflow=wf,
            run=forked_run,
            store=store,
            registry=_make_registry(),
            catalog=AssetCatalog(),
        )
        status = await ex.start_from_fork(fork_event_id=started_id, input_overrides={})
        assert status == WorkflowRunStatus.SUCCEEDED
        # All three nodes re-fired in the fork's JSONL.
        assert _node_started_names(_events(store, forked_run.run_id)) == [
            "A",
            "B",
            "C",
        ]

    async def test_overrides_on_non_completion_event_rejected(
        self, tmp_path: Path
    ) -> None:
        # Fork at a node.started event with overrides is ambiguous —
        # the spec says only completion / run.started events accept
        # overrides. The error message names the offending kind so the
        # operator knows what to do.
        store, parent_run, wf = await _run_pipeline_to_completion(tmp_path)
        events = _events(store, parent_run.run_id)
        a_started_id = next(
            e["event_id"]
            for e in events
            if e.get("kind") == "workflow.node.started"
            and (
                e.get("node_name") == "A"
                or e.get("payload", {}).get("node_name") == "A"
            )
        )

        forked_run = store.create(
            workflow_ref=parent_run.workflow_ref,
            inputs={},
            trace_id=parent_run.trace_id,
            forked_from=ForkedFrom(
                run_id=parent_run.run_id, fork_event_id=a_started_id
            ),
        )
        ex = IterativeExecutor(
            workflow=wf,
            run=forked_run,
            store=store,
            registry=_make_registry(),
            catalog=AssetCatalog(),
        )
        with pytest.raises(WorkflowRuntimeError) as exc_info:
            await ex.start_from_fork(
                fork_event_id=a_started_id,
                input_overrides={"value": 1},
            )
        msg = str(exc_info.value)
        assert "workflow.node.completed" in msg
        assert "workflow.run.started" in msg


# ---------------------------------------------------------------------------
# AC 1.3 / 1.4 — error paths
# ---------------------------------------------------------------------------


class TestForkErrorPaths:
    async def test_invalid_fork_event_id_raises_runtime_error(
        self, tmp_path: Path
    ) -> None:
        store, parent_run, wf = await _run_pipeline_to_completion(tmp_path)
        # EventId validates a UUID body; pick a syntactically-valid
        # but never-issued sentinel so the validator passes and we
        # exercise the executor's missing-event arm.
        bogus_event_id = EventId("ev_99999999-9999-4999-8999-999999999999")
        forked_run = store.create(
            workflow_ref=parent_run.workflow_ref,
            inputs={},
            trace_id=parent_run.trace_id,
            forked_from=ForkedFrom(
                run_id=parent_run.run_id,
                fork_event_id=bogus_event_id,
            ),
        )
        ex = IterativeExecutor(
            workflow=wf,
            run=forked_run,
            store=store,
            registry=_make_registry(),
            catalog=AssetCatalog(),
        )
        with pytest.raises(WorkflowRuntimeError) as exc_info:
            await ex.start_from_fork(
                fork_event_id=bogus_event_id,
                input_overrides={},
            )
        msg = str(exc_info.value)
        assert bogus_event_id in msg
        # Error message should guide users away from spawned-session ids.
        assert "session" in msg.lower() or "agent" in msg.lower()

    async def test_parent_events_jsonl_deleted_raises_run_not_found(
        self, tmp_path: Path
    ) -> None:
        store, parent_run, wf = await _run_pipeline_to_completion(tmp_path)
        # Capture a valid fork event id BEFORE we wipe the JSONL.
        b_completed_id = _completion_event_id_for(
            _events(store, parent_run.run_id), "B"
        )
        # Delete the parent's events.jsonl out from under us.
        parent_dir = store.directory(parent_run.run_id)
        parent_dir.events_path.unlink()

        forked_run = store.create(
            workflow_ref=parent_run.workflow_ref,
            inputs={},
            trace_id=parent_run.trace_id,
            forked_from=ForkedFrom(
                run_id=parent_run.run_id,
                fork_event_id=b_completed_id,
            ),
        )
        ex = IterativeExecutor(
            workflow=wf,
            run=forked_run,
            store=store,
            registry=_make_registry(),
            catalog=AssetCatalog(),
        )
        with pytest.raises(WorkflowRunNotFound) as exc_info:
            await ex.start_from_fork(fork_event_id=b_completed_id, input_overrides={})
        assert exc_info.value.run_id == str(parent_run.run_id)

    async def test_parent_meta_deleted_raises_run_not_found(
        self, tmp_path: Path
    ) -> None:
        store, parent_run, wf = await _run_pipeline_to_completion(tmp_path)
        b_completed_id = _completion_event_id_for(
            _events(store, parent_run.run_id), "B"
        )
        forked_run = store.create(
            workflow_ref=parent_run.workflow_ref,
            inputs={},
            trace_id=parent_run.trace_id,
            forked_from=ForkedFrom(
                run_id=parent_run.run_id,
                fork_event_id=b_completed_id,
            ),
        )
        # Delete the parent's meta.json so store.read raises.
        store.directory(parent_run.run_id).meta_path.unlink()
        # And the JSONL so the defensive walk doesn't crash.
        store.directory(parent_run.run_id).events_path.unlink()

        ex = IterativeExecutor(
            workflow=wf,
            run=forked_run,
            store=store,
            registry=_make_registry(),
            catalog=AssetCatalog(),
        )
        with pytest.raises(WorkflowRunNotFound):
            await ex.start_from_fork(fork_event_id=b_completed_id, input_overrides={})

    async def test_missing_forked_from_raises_value_error(self, tmp_path: Path) -> None:
        # Programming-error guard: a run without forked_from MUST NOT
        # be fork-able. ValueError (not WorkflowError) so the CLI's
        # exception arms don't accidentally render it as an envelope.
        wf = _make_pipeline_workflow()
        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/pipeline", inputs={})
        ex = IterativeExecutor(
            workflow=wf,
            run=run,
            store=store,
            registry=_make_registry(),
            catalog=AssetCatalog(),
        )
        with pytest.raises(ValueError, match="forked_from"):
            await ex.start_from_fork(fork_event_id="ev_x", input_overrides={})


# ---------------------------------------------------------------------------
# AC 1.8 — trace_id inheritance (dedicated test) +
# both parent and forked_from set
# ---------------------------------------------------------------------------


class TestForkParentCoexistence:
    async def test_run_with_both_parent_and_forked_from_executes(
        self, tmp_path: Path
    ) -> None:
        # A forked sub-workflow run carries BOTH ``parent`` (its
        # original spawning workflow-node invocation) AND ``forked_from``
        # (the fork pointer). start_from_fork must not be confused by
        # the parent pointer.
        store, parent_run, wf = await _run_pipeline_to_completion(tmp_path)
        b_completed_id = _completion_event_id_for(
            _events(store, parent_run.run_id), "B"
        )

        synthetic_parent = ParentRunPointer(
            workflow_run_id=parent_run.run_id,
            invocation_id=InvocationId("inv_11111111-1111-4111-8111-111111111111"),
        )
        forked_run = store.create(
            workflow_ref=parent_run.workflow_ref,
            inputs={},
            trace_id=parent_run.trace_id,
            parent=synthetic_parent,
            forked_from=ForkedFrom(
                run_id=parent_run.run_id,
                fork_event_id=b_completed_id,
            ),
        )
        # Both pointers survived round-trip.
        reread = store.read(forked_run.run_id)
        assert reread.parent == synthetic_parent
        assert reread.forked_from is not None

        ex = IterativeExecutor(
            workflow=wf,
            run=forked_run,
            store=store,
            registry=_make_registry(),
            catalog=AssetCatalog(),
        )
        status = await ex.start_from_fork(
            fork_event_id=b_completed_id, input_overrides={}
        )
        assert status == WorkflowRunStatus.SUCCEEDED
        # The store's reread now reflects the terminal status AND both
        # parent pointers persisted across the fork.
        final = store.read(forked_run.run_id)
        assert final.parent == synthetic_parent
        assert final.forked_from is not None


# ---------------------------------------------------------------------------
# NodeContext import is unused by the rewrite — keep it imported so a
# future test that wants to introspect context can use it without a
# fresh import dance.
# ---------------------------------------------------------------------------


_ = NodeContext
