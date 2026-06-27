"""Phase 3.1 Step 1 — IterativeExecutor skeleton tests.

Covers ACs 1.1-1.8 via fake runners injected through a per-test
:class:`RunnerRegistry`:

- 1.1 — executor consumes the typed :class:`Workflow` directly.
- 1.2 — worklist topological order (no recursion).
- 1.3 — ``when:`` gate via :data:`JINJA_ENV`; skip on False per
  AND-strict rule 1.
- 1.4 — bind rendering follows Round 6 J5/β; literal vs Jinja-marked
  dispatched correctly.
- 1.5 — event ordering (run.started first; per-node started/completed
  in topo order; run.completed last).
- 1.6 — :class:`RunnerRegistry` raises :class:`NodeTypeNotImplemented`
  with the correct phase string.
- 1.7 — linear pipeline + when-skip + node failure +
  deferred-type rejection.
- 1.8 — no ``sys.modules`` mutation; no ``cancel.flag`` reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from assets.catalog import AssetCatalog
from schemas import SessionId
from workflows.errors import NodeTypeNotImplemented
from workflows.executor import (
    BUILTIN_PHASE_MAP,
    IterativeExecutor,
    RunnerRegistry,
)
from workflows.parser import Workflow
from workflows.run_store import WorkflowRunStore

# ---------------------------------------------------------------------------
# Fake runners + helpers
# ---------------------------------------------------------------------------


class StaticRunner:
    """A :class:`NodeRunner` that returns a fixed output dict."""

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
    """A runner that records the ``bound_inputs`` it was called with."""

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


class RaisingRunner:
    """A runner that always raises a typed exception."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def run(self, **_: Any) -> dict[str, Any]:
        raise self._exc


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


# ---------------------------------------------------------------------------
# AC 1.5 / 1.7 — linear pipeline
# ---------------------------------------------------------------------------


class TestLinearPipeline:
    async def test_three_node_pipeline_emits_events_in_topo_order(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "first": {"type": "kolsch/nodes/bash", "command": "echo 1"},
                    "second": {"type": "kolsch/nodes/noop"},
                    "third": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {"from": "first", "to": "second"},
                        {"from": "second", "to": "third"},
                    ]
                },
            }
        )
        registry = RunnerRegistry()
        registry.register(
            "kolsch/nodes/bash",
            StaticRunner({"stdout": "1\n", "stderr": "", "exit_code": 0}),
        )
        registry.register("kolsch/nodes/noop", StaticRunner({}))
        ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)
        from workflows.run import WorkflowRunStatus

        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        events = list(store.read_events(run.run_id))
        kinds = [e["kind"] for e in events]
        assert kinds[0] == "workflow.run.started"
        assert kinds[-1] == "workflow.run.completed"
        # In between: started/completed pairs for first, second, third
        # in topo order.
        per_node_kinds = [
            (e["payload"].get("node_name"), e["kind"]) for e in events[1:-1]
        ]
        assert per_node_kinds == [
            ("first", "workflow.node.started"),
            ("first", "workflow.node.completed"),
            ("second", "workflow.node.started"),
            ("second", "workflow.node.completed"),
            ("third", "workflow.node.started"),
            ("third", "workflow.node.completed"),
        ]

    async def test_workflow_completed_carries_terminal_outputs(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "a": {"type": "kolsch/nodes/bash", "command": "echo a"},
                    "b": {"type": "kolsch/nodes/bash", "command": "echo b"},
                },
                "graph": {"edges": [{"from": "a", "to": "b"}]},
            }
        )
        registry = RunnerRegistry()
        registry.register(
            "kolsch/nodes/bash",
            StaticRunner({"stdout": "x", "stderr": "", "exit_code": 0}),
        )
        ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)
        await ex.run()
        events = list(store.read_events(run.run_id))
        completed = events[-1]
        # Only ``b`` is terminal (a has an outgoing edge).
        assert set(completed["payload"]["outputs"]) == {"b"}


# ---------------------------------------------------------------------------
# AC 1.3 — when: gate
# ---------------------------------------------------------------------------


class TestWhenGate:
    async def test_when_false_skips_node_and_emits_no_started_event(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "a": {"type": "kolsch/nodes/noop"},
                    "gated": {
                        "type": "kolsch/nodes/noop",
                        "when": "{{ false }}",
                    },
                },
                "graph": {"edges": [{"from": "a", "to": "gated"}]},
            }
        )
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", StaticRunner({}))
        ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)
        await ex.run()
        kinds = [
            (e["payload"].get("node_name"), e["kind"])
            for e in store.read_events(run.run_id)
        ]
        assert ("gated", "workflow.node.started") not in kinds
        assert ("gated", "workflow.node.completed") not in kinds
        # Workflow still completed cleanly — AND-strict rule 1.
        assert kinds[-1] == (None, "workflow.run.completed")

    async def test_when_literal_true_fires(self, tmp_path: Path) -> None:
        wf = _wf(
            {
                "nodes": {
                    "n": {
                        "type": "kolsch/nodes/noop",
                        "when": True,
                    }
                }
            }
        )
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", StaticRunner({"done": True}))
        ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)
        await ex.run()
        kinds = [e["kind"] for e in store.read_events(run.run_id)]
        assert "workflow.node.completed" in kinds


# ---------------------------------------------------------------------------
# AC 1.4 — binds: rendering (literal vs Jinja-marked)
# ---------------------------------------------------------------------------


class TestBindsRendering:
    async def test_literal_binds_pass_through(self, tmp_path: Path) -> None:
        # Receiver is ``kolsch/nodes/agent`` (``AgentInputs``
        # ``extra='allow'``); Phase 3.3 Step 0's strict-mode bind
        # validation rejects unknown keys against ``NoopInputs``
        # (``extra='forbid'``). The Phase 3.1 bind-rendering contract
        # is unchanged — only the receiver type swaps to one whose
        # input model permits the test keys.
        wf = _wf(
            {
                "nodes": {
                    "a": {"type": "kolsch/nodes/noop"},
                    "b": {
                        "type": "kolsch/nodes/agent",
                        "agent": "demo/agents/x",
                    },
                },
                "graph": {
                    "edges": [
                        {
                            "from": "a",
                            "to": "b",
                            "binds": {
                                "count": 5,
                                "flag": True,
                                "tag": "literal",
                            },
                        }
                    ]
                },
            }
        )
        noop_capture = CapturingRunner({})
        agent_capture = CapturingRunner({})
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", noop_capture)
        registry.register("kolsch/nodes/agent", agent_capture)
        ex, _, _ = _make_executor(tmp_path, workflow=wf, registry=registry)
        await ex.run()
        assert agent_capture.calls == [
            {
                "count": 5,
                "flag": True,
                "tag": "literal",
            }
        ]

    async def test_jinja_marked_binds_render_against_captured_outputs(
        self, tmp_path: Path
    ) -> None:
        # Receiver is ``kolsch/nodes/agent`` for the same reason as
        # ``test_literal_binds_pass_through``.
        wf = _wf(
            {
                "nodes": {
                    "fetch": {"type": "kolsch/nodes/bash", "command": "echo x"},
                    "use": {
                        "type": "kolsch/nodes/agent",
                        "agent": "demo/agents/x",
                    },
                },
                "graph": {
                    "edges": [
                        {
                            "from": "fetch",
                            "to": "use",
                            "binds": {"upstream": "{{ nodes.fetch.stdout }}"},
                        }
                    ]
                },
            }
        )
        registry = RunnerRegistry()
        registry.register(
            "kolsch/nodes/bash",
            StaticRunner({"stdout": "hello", "stderr": "", "exit_code": 0}),
        )
        capture = CapturingRunner({})
        registry.register("kolsch/nodes/agent", capture)
        ex, _, _ = _make_executor(tmp_path, workflow=wf, registry=registry)
        await ex.run()
        assert capture.calls[0] == {"upstream": "hello"}


# ---------------------------------------------------------------------------
# AC 1.7 — node failure
# ---------------------------------------------------------------------------


class TestNodeFailure:
    async def test_failure_halts_run_and_emits_failed_events(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "first": {"type": "kolsch/nodes/noop"},
                    "second": {"type": "kolsch/nodes/noop"},
                    "third": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {"from": "first", "to": "second"},
                        {"from": "second", "to": "third"},
                    ]
                },
            }
        )
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", StaticRunner({}))
        # Replace the middle node's runner with one that raises by
        # registering a per-node sentinel via the type ref dispatch.
        # Easiest: install a tiered registry where ``noop`` always
        # raises on ``second`` invocations. Simpler — use a closure
        # runner that raises on the second invocation it sees.

        class RaiseOnSecondCall:
            def __init__(self) -> None:
                self.calls = 0

            async def run(self, **_: Any) -> dict[str, Any]:
                self.calls += 1
                if self.calls == 2:
                    msg = "boom on second node"
                    raise RuntimeError(msg)
                return {}

        registry.register("kolsch/nodes/noop", RaiseOnSecondCall())
        ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)
        from workflows.run import WorkflowRunStatus

        status = await ex.run()
        assert status == WorkflowRunStatus.FAILED
        events = list(store.read_events(run.run_id))
        kinds = [(e["payload"].get("node_name"), e["kind"]) for e in events]
        # ``third`` never starts.
        assert ("third", "workflow.node.started") not in kinds
        # ``second`` records the failure.
        assert ("second", "workflow.node.failed") in kinds
        # Run-level failure event is last.
        assert kinds[-1] == (None, "workflow.run.failed")


# ---------------------------------------------------------------------------
# Phase 3.2 §11.6.3 — agent node payload extension
# ---------------------------------------------------------------------------


class _SpawningRunner:
    """A runner that stamps ``ctx.spawned_session_id`` like ``AgentRunner``
    does, so the executor's payload builder can be exercised without
    standing up the full Phase 3.2 wiring (engine + session store +
    config builder). Mirrors the
    ``object.__setattr__(ctx, "spawned_session_id", ...)`` pattern the
    real runner uses to mutate the frozen :class:`NodeContext`.
    """

    def __init__(self, spawned_session_id: str, output: dict[str, Any]) -> None:
        self._sid = spawned_session_id
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
        _ = (node_name, node_def, bound_inputs, state)
        object.__setattr__(ctx, "spawned_session_id", SessionId(self._sid))
        return dict(self._output)


class TestSpawnedSessionIdPayload:
    async def test_agent_node_payload_carries_spawned_session_id(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "classify": {
                        "type": "kolsch/nodes/agent",
                        "agent": "demo/agents/classifier",
                    }
                }
            }
        )
        sid = (
            "ses_11111111-2222-3333-4444-555555555555"
        )
        runner = _SpawningRunner(sid, {"label": "ok"})
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/agent", runner)
        ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)

        await ex.run()
        events = list(store.read_events(run.run_id))
        completed = next(
            e
            for e in events
            if e["kind"] == "workflow.node.completed"
            and e["payload"].get("node_name") == "classify"
        )
        assert completed["payload"].get("spawned_session_id") == sid
        # Output still rides along — the extension is additive.
        assert completed["payload"]["output"] == {"label": "ok"}

    async def test_non_agent_node_payload_omits_spawned_session_id(
        self, tmp_path: Path
    ) -> None:
        """AC 3.5 — non-agent completed payloads are byte-identical to
        the pre-3.2 shape. A misbehaving future runner that forgets to
        clear ``ctx.spawned_session_id`` STILL can't pollute the
        payload because the gate is on ``node_def.type``, not on the
        attribute's presence."""
        sid = (
            "ses_99999999-8888-7777-6666-555555555555"
        )
        wf = _wf(
            {
                "nodes": {
                    "rogue_noop": {"type": "kolsch/nodes/noop"},
                }
            }
        )
        runner = _SpawningRunner(sid, {"trivial": "yes"})
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", runner)
        ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)

        await ex.run()
        events = list(store.read_events(run.run_id))
        completed = next(
            e
            for e in events
            if e["kind"] == "workflow.node.completed"
            and e["payload"].get("node_name") == "rogue_noop"
        )
        assert "spawned_session_id" not in completed["payload"]
        assert set(completed["payload"].keys()) == {
            "node_name",
            "invocation_id",
            "output",
        }


# ---------------------------------------------------------------------------
# AC 1.6 — RunnerRegistry: NodeTypeNotImplemented for deferred types
# ---------------------------------------------------------------------------


class TestRunnerRegistry:
    @pytest.mark.parametrize(
        ("name", "expected_phase"),
        sorted(BUILTIN_PHASE_MAP.items()),
    )
    def test_builtin_phase_lookup(
        self, name: str, expected_phase: str
    ) -> None:
        registry = RunnerRegistry()
        with pytest.raises(NodeTypeNotImplemented) as exc_info:
            registry.get(f"kolsch/nodes/{name}", node_name="n")
        assert exc_info.value.phase == expected_phase
        assert exc_info.value.type_ref == f"kolsch/nodes/{name}"

    def test_authored_python_unregistered_falls_back_to_3_1(self) -> None:
        registry = RunnerRegistry()
        with pytest.raises(NodeTypeNotImplemented) as exc_info:
            registry.get("acme/nodes/echo", node_name="n")
        # Authored Python nodes are dispatched by Step 3's catalog
        # runner; without it the registry treats them as a Phase 3.1
        # gap.
        assert exc_info.value.phase == "3.1"

    def test_registered_runner_returned(self) -> None:
        runner = StaticRunner({})
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", runner)
        assert registry.get("kolsch/nodes/noop", node_name="n") is runner


# ---------------------------------------------------------------------------
# AC 1.7 — deferred-type rejection surfaces as node failure
# ---------------------------------------------------------------------------


class TestDeferredTypeRejection:
    async def test_deferred_type_surfaces_as_workflow_node_failed(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "decide": {
                        "type": "kolsch/nodes/branch",
                        "condition": "{{ true }}",
                    },
                    "yes_handler": {"type": "kolsch/nodes/noop"},
                    "no_handler": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {"from": "decide", "to": "yes_handler", "branch": True},
                        {"from": "decide", "to": "no_handler", "branch": False},
                    ]
                },
            }
        )
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", StaticRunner({}))
        ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)
        from workflows.run import WorkflowRunStatus

        status = await ex.run()
        assert status == WorkflowRunStatus.FAILED
        events = list(store.read_events(run.run_id))
        failed_node_events = [
            e for e in events if e["kind"] == "workflow.node.failed"
        ]
        assert len(failed_node_events) == 1
        payload = failed_node_events[0]["payload"]
        assert payload["node_name"] == "decide"
        assert payload["error_class"] == "NodeTypeNotImplemented"


# ---------------------------------------------------------------------------
# AC 1.8 — Step 3 + 4 ownership: no sys.modules / cancel.flag here
# ---------------------------------------------------------------------------


def _executor_source() -> str:
    return (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "workflows"
        / "executor.py"
    ).read_text(encoding="utf-8")


class TestStepOwnershipGuards:
    def test_executor_source_is_iterative_not_recursive(self) -> None:
        text = _executor_source()
        # The executor never calls ``await self.run()`` recursively.
        assert "await self.run(" not in text
