"""Phase 3.4 Step 1 — resume entry point tests.

Covers Step 1 ACs:

- 1.1 — :class:`WorkflowRunResumedEvent` shape.
- 1.2 — :func:`replay_into` reconstructs ``_captured`` / ``_pruned``
  / ``_completed_or_skipped`` from JSONL events.
- 1.3 — :meth:`IterativeExecutor.resume` flow: status check → replay
  → response validation → emit resumed → emit node.completed →
  re-enter worklist.
- 1.4 — resume against non-waiting status raises
  :class:`WorkflowResumeRejected`.
- 1.5 — bad response raises :class:`WorkflowInputInvalid`; status
  UNCHANGED so the operator's retry-with-correct-input succeeds.
- 1.6 — ``parent_event_id`` chains correctly across pause boundary.
- 1.7 — ``waiting_at`` sidecar removed at terminal status post-resume.
- 1.8 — ``_continue_worklist(starting_from=)`` extracted: both
  ``run()`` and ``resume()`` invoke the same dispatch loop. Verified
  implicitly via the happy-path test (resume continues a workflow
  the initial run paused on).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from assets.catalog import AssetCatalog
from workflows._replay import replay_into
from workflows.builtins import UserInputRunner
from workflows.errors import (
    WorkflowInputInvalid,
    WorkflowResumeRejected,
)
from workflows.events import (
    WorkflowEventKind,
    WorkflowRunResumedEvent,
)
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class StaticRunner:
    """Returns a fixed output dict."""

    def __init__(self, output: dict[str, Any]) -> None:
        self._output = output

    async def run(
        self,
        *,
        node_name: str,
        node_def: Any,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: Any,
    ) -> dict[str, Any]:
        _ = (node_name, node_def, bound_inputs, state, ctx)
        return dict(self._output)


class CapturingRunner:
    """Records the ``bound_inputs`` it was called with."""

    def __init__(self, output: dict[str, Any]) -> None:
        self._output = output
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        *,
        node_name: str,
        node_def: Any,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: Any,
    ) -> dict[str, Any]:
        _ = (node_name, node_def, state, ctx)
        self.calls.append(dict(bound_inputs))
        return dict(self._output)


def _wf(payload: dict[str, Any]) -> Workflow:
    return Workflow.model_validate(payload)


def _make_executor(
    tmp_path: Path,
    *,
    workflow: Workflow,
    registry: RunnerRegistry,
    inputs: dict[str, Any] | None = None,
) -> tuple[IterativeExecutor, WorkflowRunStore, Any]:
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs=inputs or {})
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=AssetCatalog(),
    )
    return ex, store, run


def _registry_with_user_input(
    *, agent_output: dict[str, Any] | None = None
) -> RunnerRegistry:
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/noop", StaticRunner({}))
    registry.register("kolsch/nodes/user_input", UserInputRunner())
    if agent_output is not None:
        registry.register(
            "kolsch/nodes/agent",
            StaticRunner(agent_output),
        )
    return registry


def _executor_for_resume(
    tmp_path: Path,
    *,
    workflow: Workflow,
    registry: RunnerRegistry,
    run_id: Any,
) -> IterativeExecutor:
    """Build a fresh executor pointed at an existing paused run.

    The CLI's resume path reads the run via ``store.read(run_id)`` and
    constructs a fresh executor. Tests mirror that pattern so the
    resume entry point operates on a freshly-instantiated executor
    (the same way the CLI's Step 2 wiring will).
    """
    store = WorkflowRunStore(tmp_path)
    run = store.read(run_id)
    return IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=AssetCatalog(),
    )


# ---------------------------------------------------------------------------
# AC 1.1 — WorkflowRunResumedEvent shape
# ---------------------------------------------------------------------------


class TestRunResumedEventShape:
    def test_event_kind_constant(self) -> None:
        assert WorkflowEventKind.RUN_RESUMED.value == "workflow.run.resumed"

    def test_serialised_event_carries_resumed_at_invocation_id(
        self,
    ) -> None:
        import uuid

        from workflows.ids import TraceId, WorkflowRunId

        ev = WorkflowRunResumedEvent(
            workflow_run_id=WorkflowRunId(f"wrk_{uuid.uuid4()}"),
            trace_id=TraceId(f"tr_{uuid.uuid4()}"),
            payload={"resumed_at_invocation_id": "inv_demo"},
        )
        serialised = ev.model_dump(mode="json")
        assert serialised["kind"] == "workflow.run.resumed"
        assert serialised["payload"]["resumed_at_invocation_id"] == "inv_demo"


# ---------------------------------------------------------------------------
# AC 1.2 — replay_into restores executor state
# ---------------------------------------------------------------------------


class TestReplayInto:
    async def test_replay_restores_completed_and_pruned_nodes(
        self, tmp_path: Path
    ) -> None:
        # Run a workflow to terminal so the JSONL has a full set of
        # node events; then replay the events into a fresh executor
        # and assert the in-memory state matches.
        wf = _wf(
            {
                "nodes": {
                    "first": {"type": "kolsch/nodes/noop"},
                    "second": {"type": "kolsch/nodes/noop"},
                },
                "graph": {"edges": [{"from": "first", "to": "second"}]},
            }
        )
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", StaticRunner({"k": "v"}))
        ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)
        await ex.run()
        events = list(store.read_events(run.run_id))

        # Build a fresh executor; replay should populate its state.
        fresh_store = WorkflowRunStore(tmp_path)
        fresh_run = fresh_store.read(run.run_id)
        fresh = IterativeExecutor(
            workflow=wf,
            run=fresh_run,
            store=fresh_store,
            registry=registry,
            catalog=AssetCatalog(),
        )
        opens = replay_into(fresh, events)
        assert fresh._captured == {
            "first": {"k": "v"},
            "second": {"k": "v"},
        }
        assert fresh._completed_or_skipped == {
            "first",
            "second",
        }
        # No open invocations — every node closed.
        assert all(v is None for v in opens.values())

    async def test_replay_identifies_open_user_input_invocation(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "prep": {"type": "kolsch/nodes/noop"},
                    "ask": {
                        "type": "kolsch/nodes/user_input",
                        "prompt": "?",
                    },
                },
                "graph": {"edges": [{"from": "prep", "to": "ask"}]},
            }
        )
        ex, store, run = _make_executor(
            tmp_path,
            workflow=wf,
            registry=_registry_with_user_input(),
        )
        await ex.run()  # pauses
        events = list(store.read_events(run.run_id))

        fresh = _executor_for_resume(
            tmp_path,
            workflow=wf,
            registry=_registry_with_user_input(),
            run_id=run.run_id,
        )
        opens = replay_into(fresh, events)
        # prep closed, ask is the open invocation.
        assert opens["prep"] is None
        assert opens["ask"] is not None
        # Cross-check: the open invocation id matches the sidecar.
        run_dir = store.directory(run.run_id)
        assert (
            opens["ask"] == run_dir.waiting_at_path.read_text(encoding="utf-8").strip()
        )

    def test_replay_raises_resume_rejected_on_failed_event(
        self, tmp_path: Path
    ) -> None:
        # Synthetic event list containing a workflow.node.failed —
        # the defensive check inside replay_into should raise.
        wf = _wf({"nodes": {"x": {"type": "kolsch/nodes/noop"}}})
        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/x")
        ex = IterativeExecutor(
            workflow=wf,
            run=run,
            store=store,
            registry=RunnerRegistry(),
            catalog=AssetCatalog(),
        )
        events = [
            {
                "kind": "workflow.node.failed",
                "node_name": "x",
                "invocation_id": "inv_x",
                "payload": {
                    "node_name": "x",
                    "invocation_id": "inv_x",
                    "error_class": "RuntimeError",
                    "message": "boom",
                },
            }
        ]
        with pytest.raises(WorkflowResumeRejected) as exc_info:
            replay_into(ex, events)
        assert exc_info.value.status == "failed"


# ---------------------------------------------------------------------------
# AC 1.4 — resume rejected when run is not waiting
# ---------------------------------------------------------------------------


class TestResumeStatusGate:
    async def test_resume_on_succeeded_run_rejects(self, tmp_path: Path) -> None:
        wf = _wf({"nodes": {"n": {"type": "kolsch/nodes/noop"}}})
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", StaticRunner({}))
        ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)
        await ex.run()  # SUCCEEDED

        # Fresh executor pointing at the succeeded run.
        fresh = _executor_for_resume(
            tmp_path,
            workflow=wf,
            registry=registry,
            run_id=run.run_id,
        )
        with pytest.raises(WorkflowResumeRejected) as exc_info:
            await fresh.resume(response={"response": "hi"})
        assert exc_info.value.status == "succeeded"
        # The succeeded run's JSONL is unchanged — no spurious
        # workflow.run.resumed event added.
        kinds = {e["kind"] for e in store.read_events(run.run_id)}
        assert "workflow.run.resumed" not in kinds

    async def test_resume_with_no_waiting_at_sidecar_rejects(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "ask": {
                        "type": "kolsch/nodes/user_input",
                        "prompt": "?",
                    }
                }
            }
        )
        ex, store, run = _make_executor(
            tmp_path,
            workflow=wf,
            registry=_registry_with_user_input(),
        )
        await ex.run()  # pauses + writes sidecar

        # Remove the sidecar to simulate an inconsistent state
        # (e.g., operator wiped it manually).
        run_dir = store.directory(run.run_id)
        run_dir.waiting_at_path.unlink()

        fresh = _executor_for_resume(
            tmp_path,
            workflow=wf,
            registry=_registry_with_user_input(),
            run_id=run.run_id,
        )
        with pytest.raises(WorkflowResumeRejected) as exc_info:
            await fresh.resume(response={"response": "x"})
        assert exc_info.value.status == "inconsistent_state"


# ---------------------------------------------------------------------------
# AC 1.3 + 1.7 + 1.8 — resume happy path
# ---------------------------------------------------------------------------


class TestResumeHappyPath:
    async def test_resume_completes_workflow_and_clears_sidecar(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "prep": {"type": "kolsch/nodes/noop"},
                    "ask": {
                        "type": "kolsch/nodes/user_input",
                        "prompt": "?",
                    },
                    "after": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {"from": "prep", "to": "ask"},
                        {"from": "ask", "to": "after"},
                    ]
                },
            }
        )
        ex, store, run = _make_executor(
            tmp_path,
            workflow=wf,
            registry=_registry_with_user_input(),
        )
        first_status = await ex.run()
        assert first_status == WorkflowRunStatus.WAITING_FOR_INPUT

        fresh = _executor_for_resume(
            tmp_path,
            workflow=wf,
            registry=_registry_with_user_input(),
            run_id=run.run_id,
        )
        final_status = await fresh.resume(response={"response": "hello"})
        assert final_status == WorkflowRunStatus.SUCCEEDED

        # Sidecar cleaned up at terminal.
        run_dir = store.directory(run.run_id)
        assert not run_dir.waiting_at_path.exists()

        # JSONL spans both processes as one continuous stream.
        events = list(store.read_events(run.run_id))
        kinds = [e["kind"] for e in events]
        assert "workflow.run.resumed" in kinds
        assert kinds[-1] == "workflow.run.completed"
        # After resume, the after node fired.
        after_completed = [
            e
            for e in events
            if e["kind"] == "workflow.node.completed"
            and e["payload"].get("node_name") == "after"
        ]
        assert len(after_completed) == 1
        # Final captured outputs include the user_input response and
        # the downstream node.
        outputs = events[-1]["payload"]["outputs"]
        assert "after" in outputs

    async def test_resume_user_input_response_lands_in_captured(
        self, tmp_path: Path
    ) -> None:
        # Downstream consumer reads the user_input's response via the
        # bound input. The receiver here is an ``agent`` node (its
        # AgentInputs allows extra keys, sidestepping the strict
        # validator's empty-output-schema rejection on noop).
        wf = _wf(
            {
                "nodes": {
                    "ask": {
                        "type": "kolsch/nodes/user_input",
                        "prompt": "?",
                    },
                    "use": {
                        "type": "kolsch/nodes/agent",
                        "agent": "demo/agents/x",
                    },
                },
                "graph": {
                    "edges": [
                        {
                            "from": "ask",
                            "to": "use",
                            "binds": {"received": ("{{ nodes.ask.response }}")},
                        }
                    ]
                },
            }
        )
        capture = CapturingRunner({})
        registry = _registry_with_user_input()
        registry.register("kolsch/nodes/agent", capture)

        ex, _store, run = _make_executor(tmp_path, workflow=wf, registry=registry)
        await ex.run()  # pauses

        registry_after = _registry_with_user_input()
        capture_after = CapturingRunner({})
        registry_after.register("kolsch/nodes/agent", capture_after)
        fresh = _executor_for_resume(
            tmp_path,
            workflow=wf,
            registry=registry_after,
            run_id=run.run_id,
        )
        await fresh.resume(response={"response": "world"})

        # The downstream receiver was bound with the user_input's
        # response — confirms replay populated ``_captured["ask"]``
        # AND the resume entry's node.completed emission stored the
        # response under the right key.
        assert capture_after.calls == [{"received": "world"}]


# ---------------------------------------------------------------------------
# AC 1.5 — bad response preserves paused status
# ---------------------------------------------------------------------------


class TestBadResponsePreservesStatus:
    async def test_missing_response_key_rejects_without_status_flip(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "ask": {
                        "type": "kolsch/nodes/user_input",
                        "prompt": "?",
                    }
                }
            }
        )
        ex, store, run = _make_executor(
            tmp_path,
            workflow=wf,
            registry=_registry_with_user_input(),
        )
        await ex.run()

        fresh = _executor_for_resume(
            tmp_path,
            workflow=wf,
            registry=_registry_with_user_input(),
            run_id=run.run_id,
        )
        with pytest.raises(WorkflowInputInvalid):
            await fresh.resume(response={"wrong_key": "x"})

        # Status preserved → operator can retry.
        loaded = store.read(run.run_id)
        assert loaded.status == WorkflowRunStatus.WAITING_FOR_INPUT
        # Sidecar still intact.
        run_dir = store.directory(run.run_id)
        assert run_dir.waiting_at_path.exists()
        # No spurious workflow.run.resumed event.
        kinds = {e["kind"] for e in store.read_events(run.run_id)}
        assert "workflow.run.resumed" not in kinds

    async def test_wrong_type_response_rejects_without_status_flip(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "ask": {
                        "type": "kolsch/nodes/user_input",
                        "prompt": "?",
                    }
                }
            }
        )
        ex, store, run = _make_executor(
            tmp_path,
            workflow=wf,
            registry=_registry_with_user_input(),
        )
        await ex.run()

        fresh = _executor_for_resume(
            tmp_path,
            workflow=wf,
            registry=_registry_with_user_input(),
            run_id=run.run_id,
        )
        with pytest.raises(WorkflowInputInvalid):
            await fresh.resume(response={"response": 123})

        loaded = store.read(run.run_id)
        assert loaded.status == WorkflowRunStatus.WAITING_FOR_INPUT

    async def test_retry_with_correct_response_after_bad_succeeds(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "ask": {
                        "type": "kolsch/nodes/user_input",
                        "prompt": "?",
                    }
                }
            }
        )
        ex, _store, run = _make_executor(
            tmp_path,
            workflow=wf,
            registry=_registry_with_user_input(),
        )
        await ex.run()

        # Bad attempt — should not flip status.
        fresh = _executor_for_resume(
            tmp_path,
            workflow=wf,
            registry=_registry_with_user_input(),
            run_id=run.run_id,
        )
        with pytest.raises(WorkflowInputInvalid):
            await fresh.resume(response={"wrong": "x"})

        # Retry with correct input — succeeds.
        fresh2 = _executor_for_resume(
            tmp_path,
            workflow=wf,
            registry=_registry_with_user_input(),
            run_id=run.run_id,
        )
        status = await fresh2.resume(response={"response": "ok"})
        assert status == WorkflowRunStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# AC 1.6 — parent_event_id chain across the pause boundary
# ---------------------------------------------------------------------------


class TestParentEventIdChain:
    async def test_resumed_event_chains_to_jsonl_tail_at_pause(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "ask": {
                        "type": "kolsch/nodes/user_input",
                        "prompt": "?",
                    }
                }
            }
        )
        ex, store, run = _make_executor(
            tmp_path,
            workflow=wf,
            registry=_registry_with_user_input(),
        )
        await ex.run()  # pauses
        pre_pause_events = list(store.read_events(run.run_id))
        pre_pause_tail_id = pre_pause_events[-1]["event_id"]
        # The pre-pause tail is the user_input's started event.
        assert pre_pause_events[-1]["kind"] == "workflow.node.started"

        fresh = _executor_for_resume(
            tmp_path,
            workflow=wf,
            registry=_registry_with_user_input(),
            run_id=run.run_id,
        )
        await fresh.resume(response={"response": "x"})

        all_events = list(store.read_events(run.run_id))
        resumed_event = next(
            e for e in all_events if e["kind"] == "workflow.run.resumed"
        )
        # AC 1.6 — the resumed event's parent_event_id IS the
        # last event in the JSONL at pause time.
        assert resumed_event["parent_event_id"] == pre_pause_tail_id
        # The user_input completion event chains to the resumed
        # event (continuous chain across processes).
        completion = next(
            e
            for e in all_events
            if e["kind"] == "workflow.node.completed"
            and e["payload"].get("node_name") == "ask"
        )
        assert completion["parent_event_id"] == resumed_event["event_id"]


# ---------------------------------------------------------------------------
# AC 1.2 — replay handles workflow.node.pruned events (Phase 3.3)
# ---------------------------------------------------------------------------


class TestReplayHandlesPruned:
    def test_replay_marks_pruned_node_and_completed_or_skipped(
        self, tmp_path: Path
    ) -> None:
        wf = _wf({"nodes": {"x": {"type": "kolsch/nodes/noop"}}})
        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/x")
        ex = IterativeExecutor(
            workflow=wf,
            run=run,
            store=store,
            registry=RunnerRegistry(),
            catalog=AssetCatalog(),
        )
        events = [
            {
                "kind": "workflow.node.pruned",
                "node_name": "downstream",
                "invocation_id": "inv_pruned",
                "payload": {
                    "node_name": "downstream",
                    "reason": "branch_not_taken",
                    "upstream_source": "source",
                    "branch_value": True,
                },
            }
        ]
        replay_into(ex, events)
        assert "downstream" in ex._pruned
        assert "downstream" in ex._completed_or_skipped
