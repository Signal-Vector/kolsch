"""Phase 3.1 Step 2 — built-in runners + timeout/retry wrappers.

Covers ACs 2.1-2.7:

- 2.1 — :class:`BashRunner` routes through
  :func:`tools.dispatch.dispatch_tool` (no direct ``subprocess.run``).
- 2.2 — :class:`NoopRunner` returns ``{}``.
- 2.3 — :func:`with_timeout` enforces the deadline; raises
  :class:`NodeTimeoutExceeded`.
- 2.4 — :func:`with_retry` consumes intermediate failures before
  emitting the final terminal event (rule 3).
- 2.5 — linear bash pipeline runs end-to-end.
- 2.6 — other built-in types still raise
  :class:`NodeTypeNotImplemented` with the correct phase.
- 2.7 — no regressions (verified at the suite level).
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any

import pytest

from assets.catalog import AssetCatalog
from providers.authz import RuleBasedAuthorizationProvider
from providers.sandbox import NativeSandboxProvider
from providers.secrets import DotenvSecretsProvider
from tools.decorator import get_tool_definition
from workflows.builtins import (
    BashRunner,
    NoopRunner,
    register_builtins,
    with_retry,
    with_timeout,
)
from workflows.context import NodeContext
from workflows.errors import NodeTimeoutExceeded, NodeTypeNotImplemented
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

# ---------------------------------------------------------------------------
# Bash tool fixture — load the starter bundle bash.py via importlib so the
# tests exercise the real @tool-decorated function. Module-scoped so the
# importlib cost is paid once.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bash_defn() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_test_kolsch_bash_tool",
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "cli"
        / "_starter_bundle"
        / "kolsch"
        / "tools"
        / "bash.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return get_tool_definition(module.bash)


# ---------------------------------------------------------------------------
# Workspace fixture — minimal bootstrap so DotenvSecretsProvider works.
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path: Path) -> Path:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    return tmp_path


def _make_registry(
    tmp_path: Path, bash_defn: Any
) -> RunnerRegistry:
    workspace = _make_workspace(tmp_path)
    reg = RunnerRegistry()
    register_builtins(
        reg,
        bash_defn=bash_defn,
        authz=RuleBasedAuthorizationProvider(
            config={
                "rules": [
                    {"who": ".*", "what": ".*", "effect": "allow"},
                ]
            }
        ),
        secrets=DotenvSecretsProvider(
            root=workspace, config={"path": ".env"}
        ),
        sandbox=NativeSandboxProvider(config={}),
    )
    return reg


async def _run_workflow(
    *,
    tmp_path: Path,
    workflow: Workflow,
    registry: RunnerRegistry,
) -> tuple[WorkflowRunStatus, list[dict[str, Any]]]:
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=AssetCatalog(),
    )
    status = await ex.run()
    return status, list(store.read_events(run.run_id))


# ---------------------------------------------------------------------------
# AC 2.2 — NoopRunner returns {}
# ---------------------------------------------------------------------------


class TestNoopRunner:
    async def test_noop_returns_empty_dict(self, tmp_path: Path) -> None:
        runner = NoopRunner()
        ctx = NodeContext.for_test()
        output = await runner.run(
            node_name="n",
            node_def=None,
            bound_inputs={},
            state={},
            ctx=ctx,
        )
        assert output == {}


# ---------------------------------------------------------------------------
# AC 2.1 / 2.5 — BashRunner routes through dispatch_tool
# ---------------------------------------------------------------------------


class TestBashRunner:
    async def test_single_bash_node_returns_stdout(
        self, tmp_path: Path, bash_defn: Any
    ) -> None:
        registry = _make_registry(tmp_path, bash_defn)
        wf = Workflow.model_validate(
            {
                "nodes": {
                    "greet": {
                        "type": "kolsch/nodes/bash",
                        "command": "echo hello",
                    }
                },
                "graph": {"edges": []},
            }
        )
        status, events = await _run_workflow(
            tmp_path=tmp_path, workflow=wf, registry=registry
        )
        assert status == WorkflowRunStatus.SUCCEEDED
        completed = [e for e in events if e["kind"] == "workflow.node.completed"]
        assert len(completed) == 1
        output = completed[0]["payload"]["output"]
        assert output["stdout"] == "hello\n"
        assert output["exit_code"] == 0

    async def test_linear_bash_pipeline_threads_outputs(
        self, tmp_path: Path, bash_defn: Any
    ) -> None:
        registry = _make_registry(tmp_path, bash_defn)
        wf = Workflow.model_validate(
            {
                "nodes": {
                    "first": {
                        "type": "kolsch/nodes/bash",
                        "command": "echo first",
                    },
                    "second": {
                        "type": "kolsch/nodes/bash",
                        "command": "echo {{ inputs.upstream }}",
                    },
                },
                "graph": {
                    "edges": [
                        {
                            "from": "first",
                            "to": "second",
                            "binds": {"upstream": "{{ nodes.first.stdout }}"},
                        }
                    ]
                },
            }
        )
        status, events = await _run_workflow(
            tmp_path=tmp_path, workflow=wf, registry=registry
        )
        assert status == WorkflowRunStatus.SUCCEEDED
        completed = [
            e
            for e in events
            if e["kind"] == "workflow.node.completed"
            and e["payload"]["node_name"] == "second"
        ]
        assert len(completed) == 1
        second_stdout = completed[0]["payload"]["output"]["stdout"]
        # The trailing newline from the first node's stdout flows
        # through Jinja into the second command's argument; the
        # second echo emits ``first\n\n`` (its own newline added on
        # top of the inherited one).
        assert "first" in second_stdout

    def test_bash_runner_does_not_use_subprocess_directly(self) -> None:
        """AC 2.1 grep guard — bash dispatch goes through the tool
        pipeline, NOT through a raw subprocess call inside the runner."""
        src = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "workflows"
            / "builtins"
            / "bash.py"
        )
        text = src.read_text(encoding="utf-8")
        assert "import subprocess" not in text
        assert "from subprocess" not in text
        assert "subprocess.run" not in text
        assert "subprocess.Popen" not in text


# ---------------------------------------------------------------------------
# AC 2.3 — with_timeout
# ---------------------------------------------------------------------------


class TestTimeoutWrapper:
    async def test_timeout_raises_node_timeout_exceeded(self) -> None:
        class _SlowRunner:
            async def run(self, **_: Any) -> dict[str, Any]:
                await asyncio.sleep(5)
                return {}

        wrapped = with_timeout(_SlowRunner(), timeout_seconds=0.1)
        ctx = NodeContext.for_test(invocation_id="inv_test_timeout")
        with pytest.raises(NodeTimeoutExceeded) as exc_info:
            await wrapped.run(
                node_name="slow",
                node_def=None,
                bound_inputs={},
                state={},
                ctx=ctx,
            )
        assert exc_info.value.node == "slow"
        assert exc_info.value.limit == pytest.approx(0.1)

    async def test_timeout_passthrough_when_runner_completes(self) -> None:
        class _FastRunner:
            async def run(self, **_: Any) -> dict[str, Any]:
                return {"value": 42}

        wrapped = with_timeout(_FastRunner(), timeout_seconds=1.0)
        ctx = NodeContext.for_test()
        output = await wrapped.run(
            node_name="fast",
            node_def=None,
            bound_inputs={},
            state={},
            ctx=ctx,
        )
        assert output == {"value": 42}


# ---------------------------------------------------------------------------
# AC 2.4 — with_retry
# ---------------------------------------------------------------------------


class TestRetryWrapper:
    async def test_retries_consume_intermediate_failures(self) -> None:
        class _FlakyRunner:
            def __init__(self) -> None:
                self.attempts = 0

            async def run(self, **_: Any) -> dict[str, Any]:
                self.attempts += 1
                if self.attempts < 3:
                    msg = f"attempt {self.attempts}"
                    raise RuntimeError(msg)
                return {"ok": True}

        flaky = _FlakyRunner()
        wrapped = with_retry(flaky, retries=3, backoff_seconds=0.01)
        ctx = NodeContext.for_test()
        output = await wrapped.run(
            node_name="flaky",
            node_def=None,
            bound_inputs={},
            state={},
            ctx=ctx,
        )
        assert output == {"ok": True}
        assert flaky.attempts == 3

    async def test_final_attempt_failure_propagates(self) -> None:
        class _AlwaysFails:
            async def run(self, **_: Any) -> dict[str, Any]:
                msg = "boom"
                raise RuntimeError(msg)

        wrapped = with_retry(_AlwaysFails(), retries=2, backoff_seconds=0.01)
        ctx = NodeContext.for_test()
        with pytest.raises(RuntimeError, match="boom"):
            await wrapped.run(
                node_name="bad",
                node_def=None,
                bound_inputs={},
                state={},
                ctx=ctx,
            )

    def test_retries_must_be_non_negative(self) -> None:
        class _R:
            async def run(self, **_: Any) -> dict[str, Any]:
                return {}

        with pytest.raises(ValueError, match="non-negative"):
            with_retry(_R(), retries=-1)


# ---------------------------------------------------------------------------
# Executor-level wiring — per-node modifiers fire uniformly
# ---------------------------------------------------------------------------


class TestExecutorAppliesModifiers:
    async def test_per_node_timeout_in_yaml_triggers_failure(
        self, tmp_path: Path, bash_defn: Any
    ) -> None:
        registry = _make_registry(tmp_path, bash_defn)
        wf = Workflow.model_validate(
            {
                "nodes": {
                    "slow": {
                        "type": "kolsch/nodes/bash",
                        "command": "sleep 2",
                        "timeout_seconds": 1,
                    }
                },
                "graph": {"edges": []},
            }
        )
        status, events = await _run_workflow(
            tmp_path=tmp_path, workflow=wf, registry=registry
        )
        assert status == WorkflowRunStatus.FAILED
        failed = [e for e in events if e["kind"] == "workflow.node.failed"]
        assert failed
        assert failed[0]["payload"]["error_class"] == "NodeTimeoutExceeded"

    async def test_per_node_retry_emits_only_final_event(
        self, tmp_path: Path
    ) -> None:
        # Use a fake runner so we can fail-then-succeed deterministically
        # without invoking the bash tool. The retry wrapper is applied
        # by the executor based on node_def.retry.
        attempts: list[int] = []

        class _FlakyRunner:
            async def run(self, **_: Any) -> dict[str, Any]:
                attempts.append(1)
                if len(attempts) < 3:
                    msg = "transient"
                    raise RuntimeError(msg)
                return {"ok": True}

        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", _FlakyRunner())

        wf = Workflow.model_validate(
            {
                "nodes": {
                    "n": {
                        "type": "kolsch/nodes/noop",
                        "retry": 5,
                    }
                },
                "graph": {"edges": []},
            }
        )
        status, events = await _run_workflow(
            tmp_path=tmp_path, workflow=wf, registry=registry
        )
        assert status == WorkflowRunStatus.SUCCEEDED
        # Only ONE node.completed event for the (eventually) successful
        # attempt — no intermediate workflow.node.failed events.
        kinds = [e["kind"] for e in events]
        assert kinds.count("workflow.node.completed") == 1
        assert kinds.count("workflow.node.failed") == 0
        assert len(attempts) == 3


# ---------------------------------------------------------------------------
# AC 2.6 — other built-in types still raise NodeTypeNotImplemented
# ---------------------------------------------------------------------------


class TestRegisteredVsDeferred:
    def test_register_builtins_only_installs_bash_and_noop(
        self, tmp_path: Path, bash_defn: Any
    ) -> None:
        registry = _make_registry(tmp_path, bash_defn)
        # Sanity: bash + noop resolve.
        assert isinstance(
            registry.get("kolsch/nodes/bash", node_name="n"), BashRunner
        )
        assert isinstance(
            registry.get("kolsch/nodes/noop", node_name="n"), NoopRunner
        )
        # Deferred types still raise.
        for name, expected_phase in (
            ("agent", "3.2"),
            ("user_input", "3.4"),
            ("read_file", "3.5"),
            ("branch", "3.6"),
            ("compute", "3.6"),
            ("map", "3.7"),
        ):
            with pytest.raises(NodeTypeNotImplemented) as exc_info:
                registry.get(f"kolsch/nodes/{name}", node_name="n")
            assert exc_info.value.phase == expected_phase
