"""Phase 3.5 Step 1 — :class:`SetStateRunner` coverage.

Covers Step 1 ACs:

- 1.1 — per-key mutation in YAML declaration order; one event per key.
- 1.2 — YAML literal pass-through vs Jinja-marked rendering.
- 1.4 — runner returns ``{}``.
- 1.5 — registered against ``kolsch/nodes/set_state``.
- 1.7 — multi-write evolution: three writes to the same key produce
  three events with values 0, 1, 2; downstream sees 2.
- master spec §11.3 — events ordered between started + completed;
  completion's ``parent_event_id`` chains to the LAST state.mutated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from assets.catalog import AssetCatalog
from workflows.builtins import SetStateRunner
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
    reg.register("kolsch/nodes/noop", _StaticRunner({}))
    return reg


def _run_executor(
    tmp_path: Path,
    *,
    workflow: Workflow,
    registry: RunnerRegistry | None = None,
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
        registry=registry or _registry(),
        catalog=AssetCatalog(),
    )
    return ex, store, run


class _StaticRunner:
    def __init__(self, output: dict[str, Any]) -> None:
        self._output = output

    async def run(self, **_: Any) -> dict[str, Any]:
        return dict(self._output)


def _state_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        e["payload"]
        for e in events
        if e["kind"] == "workflow.state.mutated"
    ]


def _state_event_envelopes(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [e for e in events if e["kind"] == "workflow.state.mutated"]


# ---------------------------------------------------------------------------
# AC 1.5 — registry
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_set_state_resolves_to_runner(self) -> None:
        from workflows.builtins import (  # noqa: PLC0415
            SetStateRunner as _SetStateRunner,
        )

        runner = _registry().get(
            "kolsch/nodes/set_state", node_name="x"
        )
        assert isinstance(runner, _SetStateRunner)


# ---------------------------------------------------------------------------
# AC 1.1, 1.4 — single + multi-key mutation, return {}
# ---------------------------------------------------------------------------


class TestBasicMutation:
    async def test_single_set_mutates_and_emits(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "init": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"x": 5},
                    },
                }
            }
        )
        ex, store, run = _run_executor(tmp_path, workflow=wf)
        await ex.run()
        assert ex._state["x"] == 5  # noqa: SLF001 — coupling documented
        events = list(store.read_events(run.run_id))
        payloads = _state_events(events)
        assert payloads == [{"key": "x", "op": "set", "value": 5}]
        # AC 1.4 — completed payload's output is {}.
        completed = next(
            e
            for e in events
            if e["kind"] == "workflow.node.completed"
            and e["node_name"] == "init"
        )
        assert completed["payload"]["output"] == {}

    async def test_multiple_keys_declaration_order(
        self, tmp_path: Path
    ) -> None:
        # AC 1.1 — z BEFORE a in YAML → z BEFORE a on the wire,
        # NOT alphabetical. Python 3.7+ dict preserves insertion.
        wf = _wf(
            {
                "nodes": {
                    "init": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"z": 1, "a": 2, "m": 3},
                    },
                }
            }
        )
        ex, store, run = _run_executor(tmp_path, workflow=wf)
        await ex.run()
        events = list(store.read_events(run.run_id))
        keys_in_order = [p["key"] for p in _state_events(events)]
        assert keys_in_order == ["z", "a", "m"]
        assert ex._state == {"z": 1, "a": 2, "m": 3}  # noqa: SLF001

    async def test_empty_keys_no_events(self, tmp_path: Path) -> None:
        # Edge case — author-allowed empty keys dict. Runner still
        # completes; no state events.
        wf = _wf(
            {
                "nodes": {
                    "init": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {},
                    },
                }
            }
        )
        ex, store, run = _run_executor(tmp_path, workflow=wf)
        await ex.run()
        events = list(store.read_events(run.run_id))
        assert _state_events(events) == []
        assert ex._state == {}  # noqa: SLF001


# ---------------------------------------------------------------------------
# AC 1.2 — YAML literal pass-through vs Jinja-marked rendering
# ---------------------------------------------------------------------------


class TestLiteralVsJinja:
    async def test_yaml_literals_pass_through_verbatim(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "init": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {
                            "flag": True,
                            "list": [1, 2, 3],
                            "obj": {"a": 1},
                            "n": None,
                            "plain": "hello",
                            "num": 42,
                        },
                    },
                }
            }
        )
        ex, store, run = _run_executor(tmp_path, workflow=wf)
        await ex.run()
        assert ex._state == {  # noqa: SLF001
            "flag": True,
            "list": [1, 2, 3],
            "obj": {"a": 1},
            "n": None,
            "plain": "hello",
            "num": 42,
        }

    async def test_jinja_expression_evaluated(
        self, tmp_path: Path
    ) -> None:
        # Per Phase 3.3 Jinja env — NativeEnvironment returns the
        # raw Python value (int), not the string "2".
        wf = _wf(
            {
                "nodes": {
                    "init": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"x": "{{ 1 + 1 }}"},
                    },
                }
            }
        )
        ex, store, run = _run_executor(tmp_path, workflow=wf)
        await ex.run()
        assert ex._state["x"] == 2  # noqa: SLF001
        payloads = _state_events(list(store.read_events(run.run_id)))
        assert payloads[0]["value"] == 2

    async def test_jinja_reads_workflow_inputs(
        self, tmp_path: Path
    ) -> None:
        # Workflow-level ``inputs.*`` lands in the run's inputs dict;
        # the executor populates ``_state`` from it at run start AND
        # exposes it via ``{{ inputs.* }}`` in the Jinja scope.
        wf = _wf(
            {
                "nodes": {
                    "init": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"msg": "{{ inputs.greeting }}!"},
                    },
                },
            }
        )
        ex, _store, _run = _run_executor(
            tmp_path, workflow=wf, inputs={"greeting": "hi"}
        )
        await ex.run()
        assert ex._state["msg"] == "hi!"  # noqa: SLF001

    async def test_mixed_literal_and_jinja(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "init": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {
                            "status": "pending",
                            "attempts": (
                                "{{ state.attempts | default(0) + 1 }}"
                            ),
                        },
                    },
                }
            }
        )
        ex, _store, _run = _run_executor(tmp_path, workflow=wf)
        await ex.run()
        assert ex._state["status"] == "pending"  # noqa: SLF001
        assert ex._state["attempts"] == 1  # noqa: SLF001


# ---------------------------------------------------------------------------
# AC 1.7 — multi-write evolution
# ---------------------------------------------------------------------------


class TestMultiWriteEvolution:
    async def test_three_writes_evolve_value(
        self, tmp_path: Path
    ) -> None:
        # The canonical "evolving variable" pattern per master spec
        # §4.3.1 — three sequential writes to ``attempts`` produce
        # events with values 0, 1, 2 and downstream sees 2.
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
        ex, store, run = _run_executor(tmp_path, workflow=wf)
        await ex.run()
        assert ex._state["attempts"] == 2  # noqa: SLF001
        events = list(store.read_events(run.run_id))
        values = [
            p["value"]
            for p in _state_events(events)
            if p["key"] == "attempts"
        ]
        assert values == [0, 1, 2]


# ---------------------------------------------------------------------------
# Master spec §11.3 — event ordering + chain
# ---------------------------------------------------------------------------


class TestEventChain:
    async def test_events_between_started_and_completed(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "init": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"x": 1, "y": 2},
                    },
                }
            }
        )
        ex, store, run = _run_executor(tmp_path, workflow=wf)
        await ex.run()
        events = list(store.read_events(run.run_id))
        node_events = [
            e
            for e in events
            if e["kind"]
            in {
                "workflow.node.started",
                "workflow.state.mutated",
                "workflow.node.completed",
            }
        ]
        kinds = [e["kind"] for e in node_events]
        assert kinds == [
            "workflow.node.started",
            "workflow.state.mutated",
            "workflow.state.mutated",
            "workflow.node.completed",
        ]

    async def test_state_event_parent_chains_to_started(
        self, tmp_path: Path
    ) -> None:
        # Master spec §11.3 — each state.mutated event's
        # parent_event_id is the node's started id (not the previous
        # state.mutated). Mirrors the workflow.node.logged pattern.
        wf = _wf(
            {
                "nodes": {
                    "init": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"a": 1, "b": 2, "c": 3},
                    },
                }
            }
        )
        ex, store, run = _run_executor(tmp_path, workflow=wf)
        await ex.run()
        events = list(store.read_events(run.run_id))
        started = next(
            e for e in events if e["kind"] == "workflow.node.started"
        )
        state_evs = _state_event_envelopes(events)
        assert len(state_evs) == 3
        for ev in state_evs:
            assert ev["parent_event_id"] == started["event_id"]

    async def test_completed_chains_to_last_state_mutated(
        self, tmp_path: Path
    ) -> None:
        # Master spec §11.3 — node.completed chains to the LAST
        # state.mutated event for the node.
        wf = _wf(
            {
                "nodes": {
                    "init": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"a": 1, "b": 2, "c": 3},
                    },
                }
            }
        )
        ex, store, run = _run_executor(tmp_path, workflow=wf)
        await ex.run()
        events = list(store.read_events(run.run_id))
        last_state = _state_event_envelopes(events)[-1]
        completed = next(
            e
            for e in events
            if e["kind"] == "workflow.node.completed"
            and e["node_name"] == "init"
        )
        assert completed["parent_event_id"] == last_state["event_id"]


# ---------------------------------------------------------------------------
# Downstream visibility
# ---------------------------------------------------------------------------


class TestDownstreamVisibility:
    async def test_downstream_node_reads_state(
        self, tmp_path: Path
    ) -> None:
        # State writes ARE observable via ``{{ state.* }}`` — exercise
        # via a second set_state node that reads + writes a derived
        # value.
        wf = _wf(
            {
                "nodes": {
                    "init": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"base": 10},
                    },
                    "derive": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {"doubled": "{{ state.base * 2 }}"},
                    },
                },
                "graph": {
                    "edges": [{"from": "init", "to": "derive"}],
                },
            }
        )
        ex, _store, _run = _run_executor(tmp_path, workflow=wf)
        await ex.run()
        assert ex._state == {"base": 10, "doubled": 20}  # noqa: SLF001


# ---------------------------------------------------------------------------
# Partial-failure (Jinja error mid-iteration)
# ---------------------------------------------------------------------------


class TestPartialFailure:
    async def test_partial_mutations_persist_on_jinja_error(
        self, tmp_path: Path
    ) -> None:
        # First key sets fine; second key references a missing
        # ``nodes.*`` and raises. The first key's mutation + event
        # MUST persist (the JSONL forensic record) even though the
        # run aborts.
        wf = _wf(
            {
                "nodes": {
                    "init": {
                        "type": "kolsch/nodes/set_state",
                        "keys": {
                            "first": 1,
                            "second": "{{ nodes.missing.value }}",
                        },
                    },
                }
            }
        )
        ex, store, run = _run_executor(tmp_path, workflow=wf)
        status = await ex.run()
        # Run failed via the existing abort path.
        from workflows.run import (  # noqa: PLC0415
            WorkflowRunStatus,
        )

        assert status == WorkflowRunStatus.FAILED
        events = list(store.read_events(run.run_id))
        # First key's event landed before the exception.
        payloads = _state_events(events)
        assert payloads == [{"key": "first", "op": "set", "value": 1}]
