"""Phase 3.3 Step 0 — ``inputs.*`` Jinja namespace tests.

Covers AC 0.1 + 0.4 + 0.7 from the Phase 3.3 build doc:

- ``_render_jinja(..., inputs_overlay=)`` extension; edge-bound keys
  overlay onto workflow-level inputs at receivers' config-string
  rendering.
- BashRunner's ``command:`` / ``cwd:`` / ``env:`` config rendering
  resolves ``{{ inputs.<key> }}`` against the merged scope (workflow
  inputs + edge-bound binds).
- Backward compat: with NO inbound edges (overlay empty),
  ``inputs.*`` still resolves to workflow-level inputs alone.

Tests use the
:class:`workflows.builtins.catalog_node_runner.CatalogNodeRunner`-style
``CapturingRunner`` shape to assert the rendered scope reaches the
runner via the captured ``bound_inputs`` AND to assert bash's own
config-string rendering picks up the overlay.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from assets.catalog import AssetCatalog
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run_store import WorkflowRunStore


class CapturingRunner:
    """Records each invocation's ``bound_inputs`` + ``ctx``."""

    def __init__(self, output: dict[str, Any]) -> None:
        self._output = output
        self.calls: list[dict[str, Any]] = []
        self.ctxs: list[Any] = []

    async def run(
        self,
        *,
        node_name: str,
        node_def: Any,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: Any,
    ) -> dict[str, Any]:
        _ = (node_name, node_def, state)
        self.calls.append(dict(bound_inputs))
        self.ctxs.append(ctx)
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


class TestInputsOverlayScope:
    async def test_render_jinja_inputs_overlay_merges_workflow_and_overlay(
        self, tmp_path: Path
    ) -> None:
        """``_render_jinja`` directly: workflow inputs are the baseline;
        ``inputs_overlay`` shadows by key. The executor's worklist threads
        ``inputs_overlay=bound_inputs`` for receivers — this test exercises
        the helper independently."""
        wf = _wf({"nodes": {"a": {"type": "kolsch/nodes/noop"}}})
        registry = RunnerRegistry()
        ex, _, _ = _make_executor(
            tmp_path,
            workflow=wf,
            registry=registry,
            inputs={"greeting": "hello", "shared": "workflow"},
        )
        # Hand-prime the run's inputs so the scope is wired without
        # walking the worklist.
        ex._state = dict(ex._run.inputs)  # noqa: SLF001
        rendered = ex._render_jinja(  # noqa: SLF001
            "{{ inputs.greeting }}-{{ inputs.shared }}-{{ inputs.bound or 'absent' }}"
        )
        assert rendered == "hello-workflow-absent"

        overlaid = ex._render_jinja(  # noqa: SLF001
            "{{ inputs.greeting }}-{{ inputs.shared }}-{{ inputs.bound }}",
            inputs_overlay={"shared": "edge", "bound": "from-edge"},
        )
        # ``shared`` was workflow-level; the overlay key shadows it.
        assert overlaid == "hello-edge-from-edge"

    async def test_render_jinja_no_overlay_preserves_phase31_scope(
        self, tmp_path: Path
    ) -> None:
        """Backward compat (AC 0.7) — calling ``_render_jinja`` WITHOUT
        ``inputs_overlay`` MUST yield the workflow-level inputs scope
        byte-identical to Phase 3.1's render."""
        wf = _wf({"nodes": {"a": {"type": "kolsch/nodes/noop"}}})
        registry = RunnerRegistry()
        ex, _, _ = _make_executor(
            tmp_path,
            workflow=wf,
            registry=registry,
            inputs={"greeting": "hi"},
        )
        ex._state = dict(ex._run.inputs)  # noqa: SLF001
        assert (
            ex._render_jinja("{{ inputs.greeting }}!")  # noqa: SLF001
            == "hi!"
        )


class TestBoundInputsExposeOverlay:
    async def test_bound_inputs_render_uses_workflow_inputs_baseline(
        self, tmp_path: Path
    ) -> None:
        """Edge binds reference workflow-level inputs via ``inputs.*``;
        the executor's ``_render_bound_inputs`` walks each inbound
        edge's Jinja-marked values. Confirms the workflow-level
        ``inputs.*`` scope reaches the bind-render call (the
        receiver's ``bound_inputs`` then carries the rendered value).

        Uses an ``AgentInputs``-typed receiver — the model has
        ``extra='allow'`` so arbitrary bind keys flow through the
        Step 0 strict-mode gate cleanly.
        """
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
                                "greeting": "{{ inputs.greeting }}",
                                "literal": 5,
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
        ex, _, _ = _make_executor(
            tmp_path,
            workflow=wf,
            registry=registry,
            inputs={"greeting": "howdy"},
        )
        await ex.run()
        # The agent receiver saw the rendered bind dict.
        assert agent_capture.calls == [
            {"greeting": "howdy", "literal": 5}
        ]


class TestBashCommandReadsOverlay:
    async def test_bash_command_reads_workflow_and_edge_inputs(
        self, tmp_path: Path
    ) -> None:
        """A bash node's ``command:`` referencing ``{{ inputs.greeting }}``
        and ``{{ inputs.source }}`` sees the merged scope: workflow-level
        ``greeting`` overlay'd by edge-bound ``source``. The fake
        bash runner records the rendered command — proves the bash
        runtime's Jinja scope includes both keys (Phase 3.3 AC 0.4)."""
        captured_commands: list[str] = []

        class FakeBashRunner:
            async def run(
                self,
                *,
                node_name: str,
                node_def: Any,
                bound_inputs: dict[str, Any],
                state: dict[str, Any],
                ctx: Any,
            ) -> dict[str, Any]:
                _ = (node_name, state)
                # Mirror BashRunner's scope build verbatim — Phase 3.3
                # AC 0.4: ``inputs.*`` = workflow_inputs + bound_inputs
                # overlay.
                from workflows.jinja_env import JINJA_ENV  # noqa: PLC0415

                scope = {
                    "inputs": {
                        **dict(
                            getattr(ctx, "workflow_inputs", {}) or {}
                        ),
                        **dict(bound_inputs),
                    },
                    "state": dict(state),
                }
                command = node_def.command
                if "{{" in command:
                    command = (
                        JINJA_ENV.from_string(command).render(**scope)
                    )
                captured_commands.append(command)
                return {"stdout": command, "stderr": "", "exit_code": 0}

        wf = _wf(
            {
                "nodes": {
                    "fan_a": {"type": "kolsch/nodes/noop"},
                    "fan_b": {"type": "kolsch/nodes/noop"},
                    "echo": {
                        "type": "kolsch/nodes/bash",
                        "command": "echo {{ inputs.greeting }} from {{ inputs.source }}",
                    },
                },
                "graph": {
                    "edges": [
                        {
                            "from": "fan_a",
                            "to": "echo",
                            "binds": {"source": "alpha"},
                        },
                        {"from": "fan_b", "to": "echo"},
                    ]
                },
            }
        )
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", CapturingRunner({}))
        registry.register("kolsch/nodes/bash", FakeBashRunner())
        ex, _, _ = _make_executor(
            tmp_path,
            workflow=wf,
            registry=registry,
            inputs={"greeting": "hello"},
        )
        from workflows.run import WorkflowRunStatus  # noqa: PLC0415

        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        # Workflow-level ``greeting`` + edge-bound ``source`` both
        # land in the rendered command.
        assert captured_commands == ["echo hello from alpha"]

    async def test_bash_edge_overlay_shadows_workflow_input(
        self, tmp_path: Path
    ) -> None:
        """An edge-bound key with the same name as a workflow-level
        input shadows it (AC 0.4 — edge wins on conflict)."""
        captured_commands: list[str] = []

        class FakeBashRunner:
            async def run(
                self,
                *,
                node_name: str,
                node_def: Any,
                bound_inputs: dict[str, Any],
                state: dict[str, Any],
                ctx: Any,
            ) -> dict[str, Any]:
                _ = (node_name, state)
                from workflows.jinja_env import JINJA_ENV  # noqa: PLC0415

                scope = {
                    "inputs": {
                        **dict(
                            getattr(ctx, "workflow_inputs", {}) or {}
                        ),
                        **dict(bound_inputs),
                    },
                    "state": dict(state),
                }
                command = node_def.command
                if "{{" in command:
                    command = (
                        JINJA_ENV.from_string(command).render(**scope)
                    )
                captured_commands.append(command)
                return {"stdout": command, "stderr": "", "exit_code": 0}

        wf = _wf(
            {
                "nodes": {
                    "a": {"type": "kolsch/nodes/noop"},
                    "echo": {
                        "type": "kolsch/nodes/bash",
                        "command": "{{ inputs.greeting }}",
                    },
                },
                "graph": {
                    "edges": [
                        {
                            "from": "a",
                            "to": "echo",
                            "binds": {"greeting": "hi from edge"},
                        }
                    ]
                },
            }
        )
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", CapturingRunner({}))
        registry.register("kolsch/nodes/bash", FakeBashRunner())
        ex, _, _ = _make_executor(
            tmp_path,
            workflow=wf,
            registry=registry,
            inputs={"greeting": "hello"},
        )
        await ex.run()
        assert captured_commands == ["hi from edge"]

    async def test_bash_no_inbound_falls_back_to_workflow_inputs(
        self, tmp_path: Path
    ) -> None:
        """Start node's bash command (no inbound edges, empty
        overlay) still resolves ``{{ inputs.greeting }}`` against
        workflow-level inputs alone (AC 0.7 backward compat)."""
        captured_commands: list[str] = []

        class FakeBashRunner:
            async def run(
                self,
                *,
                node_name: str,
                node_def: Any,
                bound_inputs: dict[str, Any],
                state: dict[str, Any],
                ctx: Any,
            ) -> dict[str, Any]:
                _ = (node_name, bound_inputs, state)
                from workflows.jinja_env import JINJA_ENV  # noqa: PLC0415

                scope = {
                    "inputs": {
                        **dict(
                            getattr(ctx, "workflow_inputs", {}) or {}
                        ),
                        **dict(bound_inputs),
                    },
                    "state": dict(state),
                }
                command = node_def.command
                if "{{" in command:
                    command = (
                        JINJA_ENV.from_string(command).render(**scope)
                    )
                captured_commands.append(command)
                return {"stdout": command, "stderr": "", "exit_code": 0}

        wf = _wf(
            {
                "nodes": {
                    "echo": {
                        "type": "kolsch/nodes/bash",
                        "command": "echo {{ inputs.greeting }}",
                    }
                }
            }
        )
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/bash", FakeBashRunner())
        ex, _, _ = _make_executor(
            tmp_path,
            workflow=wf,
            registry=registry,
            inputs={"greeting": "solo"},
        )
        await ex.run()
        assert captured_commands == ["echo solo"]
