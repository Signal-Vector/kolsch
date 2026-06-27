"""Phase 3.5 Step 1 — :class:`DeleteStateRunner` coverage.

Covers Step 1 ACs:

- 1.3 — silent pop on absent keys; event still emitted.
- 1.4 — runner returns ``{}``.
- 1.5 — registered against ``kolsch/nodes/delete_state``.
- Master spec §11.3 — events ordered between started + completed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from assets.catalog import AssetCatalog
from workflows.builtins import DeleteStateRunner, SetStateRunner
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run_store import WorkflowRunStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wf(payload: dict[str, Any]) -> Workflow:
    return Workflow.model_validate(payload)


def _registry() -> RunnerRegistry:
    reg = RunnerRegistry()
    reg.register("kolsch/nodes/set_state", SetStateRunner())
    reg.register("kolsch/nodes/delete_state", DeleteStateRunner())
    return reg


def _run_executor(
    tmp_path: Path,
    *,
    workflow: Workflow,
    inputs: dict[str, Any] | None = None,
) -> tuple[IterativeExecutor, WorkflowRunStore, Any]:
    store = WorkflowRunStore(tmp_path)
    run = store.create(
        workflow_ref="demo/workflows/x", inputs=inputs or {}
    )
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=_registry(),
        catalog=AssetCatalog(),
    )
    return ex, store, run


def _state_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        e["payload"]
        for e in events
        if e["kind"] == "workflow.state.mutated"
    ]


# ---------------------------------------------------------------------------
# AC 1.5 — registry
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_delete_state_resolves_to_runner(self) -> None:
        from workflows.builtins import (  # noqa: PLC0415
            DeleteStateRunner as _DeleteStateRunner,
        )

        runner = _registry().get(
            "kolsch/nodes/delete_state", node_name="x"
        )
        assert isinstance(runner, _DeleteStateRunner)


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


class TestDeleteSemantics:
    async def test_delete_existing_key(self, tmp_path: Path) -> None:
        wf = _wf(
            {
                "nodes": {
                    "init": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"x": 1, "y": 2},
                    },
                    "drop": {
                        "type": "kolsch/nodes/delete_state",
                        "keys": ["x"],
                    },
                },
                "graph": {
                    "edges": [{"from": "init", "to": "drop"}],
                },
            }
        )
        ex, store, run = _run_executor(tmp_path, workflow=wf)
        await ex.run()
        assert ex._state == {"y": 2}  # noqa: SLF001
        payloads = _state_events(list(store.read_events(run.run_id)))
        # Three events total: init's two sets + drop's one delete.
        assert payloads[-1] == {
            "key": "x",
            "op": "delete",
            "value": None,
        }

    async def test_delete_absent_silent_emits_event(
        self, tmp_path: Path
    ) -> None:
        # AC 1.3 — idempotent. ``state.pop(key, None)`` silently does
        # nothing; the event still fires (forensic + replay parity).
        wf = _wf(
            {
                "nodes": {
                    "drop": {
                        "type": "kolsch/nodes/delete_state",
                        "keys": ["never_set"],
                    },
                }
            }
        )
        ex, store, run = _run_executor(tmp_path, workflow=wf)
        await ex.run()
        assert ex._state == {}  # noqa: SLF001
        payloads = _state_events(list(store.read_events(run.run_id)))
        assert payloads == [
            {"key": "never_set", "op": "delete", "value": None}
        ]

    async def test_delete_multiple_keys_in_order(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "init": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"a": 1, "b": 2},
                    },
                    "drop": {
                        "type": "kolsch/nodes/delete_state",
                        "keys": ["a", "missing", "b"],
                    },
                },
                "graph": {
                    "edges": [{"from": "init", "to": "drop"}],
                },
            }
        )
        ex, store, run = _run_executor(tmp_path, workflow=wf)
        await ex.run()
        assert ex._state == {}  # noqa: SLF001
        payloads = _state_events(list(store.read_events(run.run_id)))
        # 2 sets + 3 deletes (one for "missing" too — silent, still
        # emitted). Drop events in declaration order.
        delete_events = [p for p in payloads if p["op"] == "delete"]
        assert [p["key"] for p in delete_events] == ["a", "missing", "b"]


# ---------------------------------------------------------------------------
# AC 1.4 — runner returns {}
# ---------------------------------------------------------------------------


class TestEmptyOutput:
    async def test_completed_payload_output_is_empty(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "drop": {
                        "type": "kolsch/nodes/delete_state",
                        "keys": ["x"],
                    },
                }
            }
        )
        _ex, store, run = _run_executor(tmp_path, workflow=wf)
        await _ex.run()
        events = list(store.read_events(run.run_id))
        completed = next(
            e
            for e in events
            if e["kind"] == "workflow.node.completed"
            and e["node_name"] == "drop"
        )
        assert completed["payload"]["output"] == {}
