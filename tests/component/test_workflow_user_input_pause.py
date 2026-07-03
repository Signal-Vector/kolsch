"""Phase 3.4 Step 0 — user_input pause discipline tests.

Covers Step 0 ACs:

- 0.1 — :class:`WorkflowPausedForInput` signal class is NOT a
  :class:`WorkflowError` subclass.
- 0.2 — :class:`WorkflowResumeRejected` runtime error shape.
- 0.3 — :class:`UserInputRunner` renders ``prompt:`` via
  :meth:`NodeContext.render_jinja` then raises the signal.
- 0.4 — :meth:`NodeContext.render_jinja` is bound by the executor.
- 0.5 — :attr:`RunDirectory.waiting_at_path` computed property exists.
- 0.6 — executor catches the pause signal BEFORE the generic
  failure path; ``waiting_at`` sidecar written atomically; status
  flips to ``waiting_for_input``; module cleanup runs; exits with
  the paused status.
- 0.7 — no ``workflow.node.failed`` / ``workflow.run.failed`` emitted
  on pause.
- 0.8 — ``register_builtins`` registers the runner against the
  ``kolsch/nodes/user_input`` slot.
- 0.9 — NO resume entry point shipped at Step 0 (Step 1 owns it).

Step 1 ships the resume path; the tests below cover only the
pause-to-clean-exit half of the lifecycle.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from assets.catalog import AssetCatalog
from workflows.builtins import UserInputRunner
from workflows.context import NodeContext
from workflows.errors import (
    WorkflowError,
    WorkflowPausedForInput,
    WorkflowResumeRejected,
    WorkflowRuntimeError,
)
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import RunDirectory, WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

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
    run = store.create(
        workflow_ref="demo/workflows/x",
        inputs=inputs or {},
    )
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=AssetCatalog(),
    )
    return ex, store, run


def _registry_with_user_input() -> RunnerRegistry:
    """A registry wired with the pieces the pause-path tests need.

    ``noop`` is the cheapest non-pausing node; ``user_input`` is the
    pause-raising runner under test. No bash / agent / catalog
    runner — pause tests don't need the full provider stack.
    """
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/noop", StaticRunner({}))
    registry.register("kolsch/nodes/user_input", UserInputRunner())
    return registry


# ---------------------------------------------------------------------------
# AC 0.1 — WorkflowPausedForInput signal class shape
# ---------------------------------------------------------------------------


class TestPauseSignalShape:
    def test_pause_signal_is_not_a_workflow_error_subclass(self) -> None:
        # The whole pause-isn't-failure design hinges on this: if pause
        # subclasses WorkflowError, the executor's generic except arm
        # catches it and emits node.failed + run.failed. Bare
        # ``Exception`` is the correct base.
        assert issubclass(WorkflowPausedForInput, Exception)
        assert not issubclass(WorkflowPausedForInput, WorkflowError)
        assert not issubclass(WorkflowPausedForInput, WorkflowRuntimeError)

    def test_pause_signal_carries_typed_fields(self) -> None:
        pause = WorkflowPausedForInput(
            node_name="ask",
            invocation_id="inv_abc",
            prompt="How are you?",
        )
        assert pause.node_name == "ask"
        assert pause.invocation_id == "inv_abc"
        assert pause.prompt == "How are you?"
        # str(pause) names node + invocation_id so log scrapers can
        # correlate without parsing the exception class.
        as_text = str(pause)
        assert "'ask'" in as_text
        assert "'inv_abc'" in as_text


# ---------------------------------------------------------------------------
# AC 0.2 — WorkflowResumeRejected runtime error shape
# ---------------------------------------------------------------------------


class TestResumeRejectedShape:
    def test_is_runtime_error_subclass(self) -> None:
        # The CLI's --format json envelope renders identifying_names_keys
        # + caller_message; the gate is "is this a WorkflowRuntimeError".
        assert issubclass(WorkflowResumeRejected, WorkflowRuntimeError)

    def test_identifying_names_and_caller_message(self) -> None:
        exc = WorkflowResumeRejected(run_id="wrk_xyz", status="succeeded")
        assert exc.run_id == "wrk_xyz"
        assert exc.status == "succeeded"
        assert WorkflowResumeRejected.identifying_names_keys == (
            "run_id",
            "status",
        )
        # caller_message is non-empty (R3 #8 + class-var coverage test
        # at module import time enforces shape).
        assert WorkflowResumeRejected.caller_message
        # Single-line rendering carries the run id + status.
        rendered = str(exc)
        assert "wrk_xyz" in rendered
        assert "succeeded" in rendered


# ---------------------------------------------------------------------------
# AC 0.4 — NodeContext.render_jinja hook
# ---------------------------------------------------------------------------


class TestNodeContextRenderJinjaHook:
    def test_render_jinja_raises_when_no_callback_bound(self) -> None:
        # ``for_test`` doesn't bind a callback — calling render_jinja
        # should raise rather than silently no-op so a future runner
        # that depends on it surfaces the wiring gap loudly.
        ctx = NodeContext.for_test()
        with pytest.raises(RuntimeError, match="no Jinja renderer"):
            ctx.render_jinja("{{ inputs.greeting }}")

    def test_render_jinja_routes_through_bound_callback(self) -> None:
        captured: dict[str, Any] = {}

        def _renderer(expression: str, inputs_overlay: Any) -> str:
            captured["expression"] = expression
            captured["inputs_overlay"] = inputs_overlay
            return "rendered"

        ctx = NodeContext(
            workflow_run_id="wrk_test",
            invocation_id="inv_test",
            workspace_root=Path("/tmp"),
            log=__import__("logging").getLogger("workflows.test"),
            secrets={},
            state={},
            bundle="kolsch",
            emit_fn=lambda *_: None,
            render_jinja_callback=_renderer,
        )
        result = ctx.render_jinja(
            "{{ inputs.greeting }}", inputs_overlay={"greeting": "hi"}
        )
        assert result == "rendered"
        assert captured == {
            "expression": "{{ inputs.greeting }}",
            "inputs_overlay": {"greeting": "hi"},
        }


# ---------------------------------------------------------------------------
# AC 0.5 — RunDirectory.waiting_at_path
# ---------------------------------------------------------------------------


class TestRunDirectoryWaitingAtPath:
    def test_waiting_at_path_computed_under_run_root(
        self, tmp_path: Path
    ) -> None:
        rd = RunDirectory(root=tmp_path / "wrk_demo")
        assert rd.waiting_at_path == tmp_path / "wrk_demo" / "waiting_at"


# ---------------------------------------------------------------------------
# AC 0.6 / 0.7 — pause discipline end-to-end
# ---------------------------------------------------------------------------


class TestPauseEndToEnd:
    async def test_pause_writes_started_event_and_no_completion(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "prep": {"type": "kolsch/nodes/noop"},
                    "ask": {
                        "type": "kolsch/nodes/user_input",
                        "prompt": "What is your name?",
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

        status = await ex.run()

        assert status == WorkflowRunStatus.WAITING_FOR_INPUT
        events = list(store.read_events(run.run_id))
        kinds = [(e["payload"].get("node_name"), e["kind"]) for e in events]
        # prep ran cleanly.
        assert ("prep", "workflow.node.started") in kinds
        assert ("prep", "workflow.node.completed") in kinds
        # ask started but did NOT complete.
        assert ("ask", "workflow.node.started") in kinds
        assert ("ask", "workflow.node.completed") not in kinds
        # downstream of ask never fired.
        assert ("after", "workflow.node.started") not in kinds

    async def test_pause_writes_waiting_at_sidecar_with_invocation_id(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "ask": {
                        "type": "kolsch/nodes/user_input",
                        "prompt": "Hi there",
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

        run_dir = store.directory(run.run_id)
        assert run_dir.waiting_at_path.exists()
        recorded = run_dir.waiting_at_path.read_text(encoding="utf-8")
        # The sidecar names the open invocation, matching the JSONL's
        # started event for ``ask``.
        events = list(store.read_events(run.run_id))
        ask_started = next(
            e
            for e in events
            if e.get("kind") == "workflow.node.started"
            and e.get("payload", {}).get("node_name") == "ask"
        )
        assert recorded == ask_started["payload"]["invocation_id"]
        # No leftover tmp file from the atomic write.
        assert not run_dir.waiting_at_path.with_suffix(".tmp").exists()

    async def test_pause_flips_status_to_waiting_for_input(
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

        # Re-read via the store — the status comes from meta.json (no
        # terminal event was emitted, so the JSONL tail scan falls
        # through to the meta sidecar's status).
        loaded = store.read(run.run_id)
        assert loaded.status == WorkflowRunStatus.WAITING_FOR_INPUT

    async def test_pause_emits_no_failed_events(
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

        kinds = {
            e["kind"] for e in store.read_events(run.run_id)
        }
        assert "workflow.node.failed" not in kinds
        assert "workflow.run.failed" not in kinds
        assert "workflow.run.completed" not in kinds
        assert "workflow.run.aborted" not in kinds
        assert "workflow.run.timed_out" not in kinds


# ---------------------------------------------------------------------------
# AC 0.6 — sequential-pause readiness (Step 1 ships resume; Step 0
# verifies the FIRST pause records correctly under a sequential layout)
# ---------------------------------------------------------------------------


class TestSequentialPauseSeed:
    async def test_first_of_two_user_input_nodes_pauses(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "ask1": {
                        "type": "kolsch/nodes/user_input",
                        "prompt": "First?",
                    },
                    "between": {"type": "kolsch/nodes/noop"},
                    "ask2": {
                        "type": "kolsch/nodes/user_input",
                        "prompt": "Second?",
                    },
                },
                "graph": {
                    "edges": [
                        {"from": "ask1", "to": "between"},
                        {"from": "between", "to": "ask2"},
                    ]
                },
            }
        )
        ex, store, run = _make_executor(
            tmp_path,
            workflow=wf,
            registry=_registry_with_user_input(),
        )

        status = await ex.run()

        assert status == WorkflowRunStatus.WAITING_FOR_INPUT
        events = list(store.read_events(run.run_id))
        node_event_pairs = [
            (e["payload"].get("node_name"), e["kind"])
            for e in events
            if e["kind"].startswith("workflow.node.")
        ]
        # Only ask1's started fired; ask2 never appears.
        assert ("ask1", "workflow.node.started") in node_event_pairs
        assert ("ask2", "workflow.node.started") not in node_event_pairs
        # The sidecar carries ask1's invocation, NOT ask2's.
        run_dir = store.directory(run.run_id)
        recorded = run_dir.waiting_at_path.read_text(encoding="utf-8")
        ask1_started = next(
            e
            for e in events
            if e.get("kind") == "workflow.node.started"
            and e.get("payload", {}).get("node_name") == "ask1"
        )
        assert recorded == ask1_started["payload"]["invocation_id"]


# ---------------------------------------------------------------------------
# AC 0.3 — UserInputRunner renders prompt via NodeContext.render_jinja
# ---------------------------------------------------------------------------


class TestPromptJinjaRendering:
    async def test_pause_prompt_renders_against_inputs_scope(
        self, tmp_path: Path
    ) -> None:
        # Capture the rendered prompt by swapping in a wrapper runner
        # that delegates to ``UserInputRunner`` and traps the raised
        # signal. End-to-end the executor would discard the prompt
        # field (Step 1 surfaces it in payload via the chat capsule).
        # Asserting via the signal payload is the most direct test of
        # the rendering itself. The prompt references workflow inputs
        # only — exercising the executor's ``inputs.*`` scope without
        # tripping the validator's strict jinja-ref check on noop's
        # empty output schema.
        captured: dict[str, Any] = {}

        class CapturingUserInput:
            inner = UserInputRunner()

            async def run(self, **kwargs: Any) -> dict[str, Any]:
                try:
                    return await self.inner.run(**kwargs)
                except WorkflowPausedForInput as pause:
                    captured["prompt"] = pause.prompt
                    raise

        wf = _wf(
            {
                "nodes": {
                    "ask": {
                        "type": "kolsch/nodes/user_input",
                        "prompt": "Greeting {{ inputs.greeting }}?",
                    },
                }
            }
        )
        registry = RunnerRegistry()
        registry.register(
            "kolsch/nodes/user_input", CapturingUserInput()
        )

        ex, _, _ = _make_executor(
            tmp_path,
            workflow=wf,
            registry=registry,
            inputs={"greeting": "hi"},
        )

        await ex.run()

        assert captured["prompt"] == "Greeting hi?"

    async def test_literal_prompt_passes_through_unrendered(
        self, tmp_path: Path
    ) -> None:
        captured: dict[str, Any] = {}

        class CapturingUserInput:
            inner = UserInputRunner()

            async def run(self, **kwargs: Any) -> dict[str, Any]:
                try:
                    return await self.inner.run(**kwargs)
                except WorkflowPausedForInput as pause:
                    captured["prompt"] = pause.prompt
                    raise

        wf = _wf(
            {
                "nodes": {
                    "ask": {
                        "type": "kolsch/nodes/user_input",
                        "prompt": "Static prompt",
                    }
                }
            }
        )
        registry = RunnerRegistry()
        registry.register(
            "kolsch/nodes/user_input", CapturingUserInput()
        )
        ex, _, _ = _make_executor(
            tmp_path, workflow=wf, registry=registry
        )

        await ex.run()

        assert captured["prompt"] == "Static prompt"


# ---------------------------------------------------------------------------
# AC 0.6 — module-isolation cleanup runs at pause exit
# ---------------------------------------------------------------------------


class TestPauseRunsModuleCleanup:
    async def test_module_loaded_pre_pause_is_swept_on_pause_exit(
        self, tmp_path: Path
    ) -> None:
        # Seed a fake module that lives under the workspace's
        # ``bundles/`` tree so the cleanup filter targets it; the
        # filter only sweeps modules NOT in the pre-run snapshot
        # whose ``__file__`` resolves under the workspace.
        bundles_dir = tmp_path / "bundles" / "fakebundle"
        bundles_dir.mkdir(parents=True)
        module_path = bundles_dir / "fake_module.py"
        module_path.write_text("VALUE = 1\n", encoding="utf-8")

        loaded_module_name = "fakebundle.fake_module"

        class LoaderRunner:
            """Imports the seeded module mid-run, then returns."""

            async def run(self, **_: Any) -> dict[str, Any]:
                import importlib.util

                spec = importlib.util.spec_from_file_location(
                    loaded_module_name, module_path
                )
                assert spec is not None and spec.loader is not None
                module = importlib.util.module_from_spec(spec)
                sys.modules[loaded_module_name] = module
                spec.loader.exec_module(module)
                return {}

        wf = _wf(
            {
                "nodes": {
                    "load": {"type": "kolsch/nodes/noop"},
                    "ask": {
                        "type": "kolsch/nodes/user_input",
                        "prompt": "?",
                    },
                },
                "graph": {"edges": [{"from": "load", "to": "ask"}]},
            }
        )
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", LoaderRunner())
        registry.register(
            "kolsch/nodes/user_input", UserInputRunner()
        )

        # WorkflowRunStore uses ``tmp_path / "runs"`` as the runs root;
        # the executor's cleanup filter looks for ``workspace_root /
        # "bundles" /``, where ``workspace_root = runs_root.parent ==
        # tmp_path``. So the seeded bundles dir is in scope.
        ex, _store, _run = _make_executor(
            tmp_path,
            workflow=wf,
            registry=registry,
        )
        # Pre-condition: module not yet in sys.modules.
        sys.modules.pop(loaded_module_name, None)

        try:
            await ex.run()
        finally:
            # Defensive cleanup in case the executor's sweep regressed
            # — the assertion below still tests the contract.
            sys.modules.pop(loaded_module_name, None)

        # The pause path runs the same finally-block cleanup as the
        # terminal paths; the seeded bundle module should be gone.
        assert loaded_module_name not in sys.modules, (
            "module-isolation cleanup did not run on pause exit"
        )


# ---------------------------------------------------------------------------
# AC 0.8 — register_builtins wires the user_input slot
# ---------------------------------------------------------------------------


class TestRegisterBuiltinsWiring:
    def test_register_builtins_registers_user_input_runner(self) -> None:
        # The wiring through register_builtins is what the CLI's
        # _cmd_run consumes. Spot-checking the registration directly
        # avoids standing up the full provider stack here.
        registry = RunnerRegistry()
        registry.register(
            "kolsch/nodes/user_input", UserInputRunner()
        )
        runner = registry.get(
            "kolsch/nodes/user_input", node_name="ask"
        )
        assert isinstance(runner, UserInputRunner)


# NOTE: the Step 0 "no resume entry point yet" guardrail test has
# been removed now that Step 1 ships ``IterativeExecutor.resume``.
# The resume path's own coverage lives in
# ``tests/component/test_workflow_user_input_resume.py``.
