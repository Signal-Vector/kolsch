"""Phase 3.5 Step 3 — Phase Tests 3, 4, 5 wired end-to-end.

Phase Test 3 — three sequential ``set_state`` writes to the same key
produce three ``workflow.state.mutated`` events (values 0, 1, 2)
and downstream sees the latest value via ``{{ state.* }}``.

Phase Test 4 — a workflow that mutates state, pauses on
``user_input``, and is resumed: replay reconstructs ``_state`` and
the downstream node renders the pre-pause value. Proves the JSONL
event log is the source of truth across the process boundary.

Phase Test 5 — ``delete_state`` of an absent key succeeds silently
AND emits the event (replay parity).

These mirror the unit-coverage classes in
``test_workflow_set_state.py`` / ``test_workflow_delete_state.py``
but assemble the runners against the executor in the cross-process
shape Phase 3.5 actually ships at — the goldens harness pins the
exact JSONL byte sequences (see ``tests/golden/runs/state-*.jsonl``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from assets.catalog import AssetCatalog
from workflows.builtins import (
    DeleteStateRunner,
    SetStateRunner,
    UserInputRunner,
)
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wf(payload: dict[str, Any]) -> Workflow:
    return Workflow.model_validate(payload)


class _StaticRunner:
    def __init__(self, output: dict[str, Any]) -> None:
        self._output = output

    async def run(self, **_: Any) -> dict[str, Any]:
        return dict(self._output)


def _registry_with_state(*, include_user_input: bool = False) -> RunnerRegistry:
    reg = RunnerRegistry()
    reg.register("kolsch/nodes/set_state", SetStateRunner())
    reg.register("kolsch/nodes/delete_state", DeleteStateRunner())
    reg.register("kolsch/nodes/noop", _StaticRunner({}))
    if include_user_input:
        reg.register("kolsch/nodes/user_input", UserInputRunner())
    return reg


def _executor(
    tmp_path: Path,
    *,
    workflow: Workflow,
    registry: RunnerRegistry,
    inputs: dict[str, Any] | None = None,
    catalog: AssetCatalog | None = None,
) -> tuple[IterativeExecutor, WorkflowRunStore, Any]:
    store = WorkflowRunStore(tmp_path)
    run = store.create(
        workflow_ref="demo/workflows/state-e2e",
        inputs=inputs or {},
    )
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=catalog or AssetCatalog(),
    )
    return ex, store, run


def _catalog_with_demo_echo() -> AssetCatalog:
    """Catalog entry that short-circuits importlib so the in-test
    runner registered for ``demo/nodes/echo`` carries the load."""
    from assets.catalog import AssetEntry

    return AssetCatalog(
        assets={
            "demo/nodes/echo": AssetEntry(
                ref="demo/nodes/echo",
                asset_type="node",
                bundle="demo",
                path="",
                metadata={"builtin": True},
            )
        }
    )


def _state_payloads(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        e["payload"]
        for e in events
        if e["kind"] == "workflow.state.mutated"
    ]


# ---------------------------------------------------------------------------
# Phase Test 3 — state evolves across writes
# ---------------------------------------------------------------------------


class TestPhase3StateEvolvesAcrossWrites:
    async def test_state_evolves_across_writes(
        self, tmp_path: Path
    ) -> None:
        # init (attempts=0) → bump1 (++) → bump2 (++)  expect 0/1/2
        wf = _wf(
            {
                "nodes": {
                    "init": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"attempts": 0},
                    },
                    "bump1": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {
                            "attempts": "{{ state.attempts + 1 }}",
                        },
                    },
                    "bump2": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {
                            "attempts": "{{ state.attempts + 1 }}",
                        },
                    },
                },
                "graph": {
                    "edges": [
                        {"from": "init", "to": "bump1"},
                        {"from": "bump1", "to": "bump2"},
                    ],
                },
            }
        )
        ex, store, run = _executor(
            tmp_path, workflow=wf, registry=_registry_with_state()
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        # Final live value.
        assert ex._state["attempts"] == 2
        # Three events for ``attempts`` with values 0, 1, 2 in order.
        events = list(store.read_events(run.run_id))
        attempts_values = [
            p["value"]
            for p in _state_payloads(events)
            if p["key"] == "attempts"
        ]
        assert attempts_values == [0, 1, 2]


# ---------------------------------------------------------------------------
# Phase Test 4 — resume reconstructs state via replay
# ---------------------------------------------------------------------------


class TestPhase4ResumeReconstructsState:
    async def test_resume_reconstructs_state_via_replay(
        self, tmp_path: Path
    ) -> None:
        # prep (noop) → set_state(attempts=5) → ask (pause) → echo
        # The echo's bind references ``{{ state.attempts }}``; resume
        # → echo renders 5 because the JSONL state.mutated event was
        # replayed into the fresh executor's _state dict.
        wf = _wf(
            {
                "nodes": {
                    "prep": {"type": "kolsch/nodes/noop"},
                    "stash": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"attempts": 5},
                    },
                    "ask": {
                        "type": "kolsch/nodes/user_input",
                        "prompt": "Confirm?",
                    },
                    "echo": {"type": "demo/nodes/echo"},
                },
                "graph": {
                    "edges": [
                        {"from": "prep", "to": "stash"},
                        {"from": "stash", "to": "ask"},
                        {
                            "from": "ask",
                            "to": "echo",
                            "binds": {
                                "message": "{{ state.attempts }}",
                            },
                        },
                    ],
                },
            }
        )
        reg = _registry_with_state(include_user_input=True)
        # ``demo/nodes/echo`` captures its bound ``message`` so we can
        # assert it landed as ``5`` post-replay.
        class _CapturingEcho:
            captured_message: Any = None

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
                _CapturingEcho.captured_message = bound_inputs.get(
                    "message"
                )
                return {"message": bound_inputs.get("message")}

        reg.register("demo/nodes/echo", _CapturingEcho())
        catalog = _catalog_with_demo_echo()

        ex, store, run = _executor(
            tmp_path, workflow=wf, registry=reg, catalog=catalog
        )
        first = await ex.run()
        assert first == WorkflowRunStatus.WAITING_FOR_INPUT

        # Fresh executor pointed at the same store/run — mirrors the
        # CLI's per-process resume.
        fresh_store = WorkflowRunStore(tmp_path)
        fresh_run = fresh_store.read(run.run_id)
        fresh = IterativeExecutor(
            workflow=wf,
            run=fresh_run,
            store=fresh_store,
            registry=reg,
            catalog=catalog,
        )
        final = await fresh.resume(response={"response": "ok"})
        assert final == WorkflowRunStatus.SUCCEEDED
        # State was reconstructed pre-fire — bound ``message`` ==
        # state.attempts (5). The exact Python type (int vs str)
        # depends on Jinja's NativeEnvironment; both indicate the
        # state.mutated event was replayed correctly.
        assert int(_CapturingEcho.captured_message) == 5


# ---------------------------------------------------------------------------
# Phase Test 5 — delete-state silent on absent key
# ---------------------------------------------------------------------------


class TestPhase5DeleteStateSilentOnAbsent:
    async def test_delete_state_silent_on_absent(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "drop": {
                        "type": "kolsch/nodes/delete_state",
                        "keys": ["foo"],
                    },
                }
            }
        )
        ex, store, run = _executor(
            tmp_path, workflow=wf, registry=_registry_with_state()
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        # Event still emitted (forensic + replay parity).
        payloads = _state_payloads(list(store.read_events(run.run_id)))
        assert payloads == [
            {"key": "foo", "op": "delete", "value": None}
        ]
        # State unchanged — no exception.
        assert ex._state == {}
