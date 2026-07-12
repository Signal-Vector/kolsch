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
from typing import TYPE_CHECKING, Any, cast

import pytest

from assets.catalog import AssetCatalog
from providers.authz import RuleBasedAuthorizationProvider
from providers.sandbox import NativeSandboxProvider
from providers.secrets import DotenvSecretsProvider
from store import SqliteBackend
from store.session_store import SessionStore
from tools.decorator import get_tool_definition
from workflows.builtins import (
    AgentRunner,
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

if TYPE_CHECKING:
    from engine import AgentEngine
    from schemas import ResolvedAgentConfig

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


def _make_registry(tmp_path: Path, bash_defn: Any) -> RunnerRegistry:
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
        secrets=DotenvSecretsProvider(root=workspace, config={"path": ".env"}),
        sandbox=NativeSandboxProvider(config={}),
    )
    return reg


async def _run_workflow(
    *,
    tmp_path: Path,
    workflow: Workflow,
    registry: RunnerRegistry,
    catalog: AssetCatalog | None = None,
) -> tuple[WorkflowRunStatus, list[dict[str, Any]]]:
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=catalog if catalog is not None else AssetCatalog(),
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
# Regression: bug-workflow-policy-not-enforced end-to-end.
#
# The unit tests in tests/unit/test_dispatch_workflow_permissions.py verify
# that dispatch_tool FORWARDS workflow_permissions correctly to authz.allow
# as the first layer. This suite exercises the DOWNSTREAM contract: that
# a real RuleBasedAuthorizationProvider walks the layered rule stack in
# proximate-first order (workflow → agent → bundle → workspace) and lets
# a workflow.policy allow beat a workspace-level deny.
#
# Without this coverage the unit tests could pass while the provider
# silently ignored the workflow layer at eval time.
# ---------------------------------------------------------------------------


def _minimal_bash_catalog() -> AssetCatalog:
    """Catalog with just `kolsch/tools/bash` so new-shape rules validate.

    ``RuleBasedAuthorizationProvider`` validates new-shape refs
    (like ``kolsch/tools/bash``) against the catalog at construction
    and raises ``AuthzToolRefUnknown`` when the ref doesn't resolve.
    ``_make_registry`` sidesteps this by using legacy shape
    (``who: .*, what: .*``); tests that assert on canonical
    new-shape matches need the catalog to include the referenced
    tool.
    """
    from assets.catalog import AssetEntry, BundleManifest

    return AssetCatalog(
        bundles={
            "kolsch": BundleManifest(
                name="kolsch",
                version="0.1.0",
                description="test fixture",
                path="/tmp/bundles/kolsch",
            )
        },
        assets={
            "kolsch/tools/bash": AssetEntry(
                ref="kolsch/tools/bash",
                asset_type="tool",
                bundle="kolsch",
                path="bundles/kolsch/tools/bash.py",
            )
        },
    )


def _stamp_bash_defn(bash_defn: Any) -> Any:
    """Stamp ``home_bundle="kolsch"`` matching the workflow-runtime fix.

    The module-scope ``bash_defn`` fixture in this file (used by every
    other test class) predates the workflow-runtime home_bundle stamp
    (kolsch commit ``347fd95``) and constructs the defn directly via
    ``get_tool_definition`` without stamping. That leaves
    ``defn.action_name`` as the legacy ``tool.bash`` form — fine for
    tests that use legacy-shape ``who: .*, what: .*`` rules, but this
    class asserts against the canonical ``kolsch/tools/bash`` rule
    shape and needs the stamped defn.
    """
    bash_defn.home_bundle = "kolsch"
    return bash_defn


def _make_registry_with_workspace_deny(
    tmp_path: Path, bash_defn: Any
) -> RunnerRegistry:
    """Registry whose workspace authz has a bash DENY at the head.

    The default `_make_registry` allows everything. This variant
    installs a `{what: "kolsch/tools/bash", pattern: "*", effect: "deny"}`
    rule so any bash invocation without a workflow.policy override
    fails. The workflow-policy-wins test below relies on this.
    """
    bash_defn = _stamp_bash_defn(bash_defn)
    workspace = _make_workspace(tmp_path)
    reg = RunnerRegistry()
    register_builtins(
        reg,
        bash_defn=bash_defn,
        authz=RuleBasedAuthorizationProvider(
            config={
                "rules": [
                    # Workspace-level deny for bash — every bash
                    # invocation without a beating workflow layer
                    # must hit this.
                    {
                        "what": "kolsch/tools/bash",
                        "pattern": "*",
                        "effect": "deny",
                    },
                    # Everything else stays allow so the surrounding
                    # workflow pre-flight (session start, etc.)
                    # doesn't accidentally trip on some other rule.
                    {"who": ".*", "what": ".*", "effect": "allow"},
                ]
            },
            catalog=_minimal_bash_catalog(),
        ),
        secrets=DotenvSecretsProvider(root=workspace, config={"path": ".env"}),
        sandbox=NativeSandboxProvider(config={}),
    )
    return reg


class TestWorkflowPolicyEnforcement:
    """Real-provider end-to-end coverage for bug-workflow-policy-not-enforced."""

    async def test_workflow_policy_allow_beats_workspace_deny(
        self, tmp_path: Path, bash_defn: Any
    ) -> None:
        """workflow.policy allow at layer 0 beats workspace deny at the tail.

        The provider walks layers in proximate-first order (workflow →
        workspace) and short-circuits on the first matching rule. This
        is the entire point of the policy field.
        """
        registry = _make_registry_with_workspace_deny(tmp_path, bash_defn)
        wf = Workflow.model_validate(
            {
                "nodes": {
                    "greet": {
                        "type": "kolsch/nodes/bash",
                        "command": "echo hello",
                    }
                },
                "policy": [
                    {
                        "what": "kolsch/tools/bash",
                        "pattern": "*",
                        "effect": "allow",
                    }
                ],
                "graph": {"edges": []},
            }
        )
        status, events = await _run_workflow(
            tmp_path=tmp_path,
            workflow=wf,
            registry=registry,
            catalog=_minimal_bash_catalog(),
        )
        assert status == WorkflowRunStatus.SUCCEEDED, (
            "workflow.policy allow at layer 0 must beat workspace deny; "
            f"got status={status.value!r}. events={events[-3:]}"
        )
        completed = [e for e in events if e["kind"] == "workflow.node.completed"]
        assert len(completed) == 1
        assert completed[0]["payload"]["output"]["stdout"] == "hello\n"

    async def test_no_workflow_policy_still_hits_workspace_deny(
        self, tmp_path: Path, bash_defn: Any
    ) -> None:
        """Negative control: absent workflow.policy, the workspace deny fires.

        Pins that the deny rule in the workspace ACTUALLY works —
        otherwise the positive test above would pass for the wrong
        reason (e.g., a stray allow-all elsewhere).
        """
        registry = _make_registry_with_workspace_deny(tmp_path, bash_defn)
        wf = Workflow.model_validate(
            {
                "nodes": {
                    "greet": {
                        "type": "kolsch/nodes/bash",
                        "command": "echo hello",
                    }
                },
                # NO policy: block.
                "graph": {"edges": []},
            }
        )
        status, events = await _run_workflow(
            tmp_path=tmp_path,
            workflow=wf,
            registry=registry,
            catalog=_minimal_bash_catalog(),
        )
        assert status == WorkflowRunStatus.FAILED
        failed = [e for e in events if e["kind"] == "workflow.node.failed"]
        assert len(failed) == 1
        assert "Authorization denied" in failed[0]["payload"]["message"]

    async def test_workflow_policy_deny_beats_workspace_allow(
        self, tmp_path: Path, bash_defn: Any
    ) -> None:
        """Symmetric case: workflow.policy deny wins even against workspace allow.

        Locks the proximate-first semantics both ways — a workflow
        can TIGHTEN what the workspace allows, not only widen.
        """
        # Reuse the default all-allow workspace; add a workflow.policy
        # deny at layer 0. Bash defn needs the kolsch stamp so the
        # canonical `what: kolsch/tools/bash` deny rule matches.
        registry = _make_registry(tmp_path, _stamp_bash_defn(bash_defn))
        wf = Workflow.model_validate(
            {
                "nodes": {
                    "greet": {
                        "type": "kolsch/nodes/bash",
                        "command": "echo hello",
                    }
                },
                "policy": [
                    {
                        "what": "kolsch/tools/bash",
                        "pattern": "*",
                        "effect": "deny",
                    }
                ],
                "graph": {"edges": []},
            }
        )
        status, events = await _run_workflow(
            tmp_path=tmp_path,
            workflow=wf,
            registry=registry,
            catalog=_minimal_bash_catalog(),
        )
        assert status == WorkflowRunStatus.FAILED
        failed = [e for e in events if e["kind"] == "workflow.node.failed"]
        assert len(failed) == 1
        assert "Authorization denied" in failed[0]["payload"]["message"]


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

    async def test_per_node_retry_emits_only_final_event(self, tmp_path: Path) -> None:
        # Use a mock runner so we can fail-then-succeed deterministically
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
        from workflows.builtins import UserInputRunner

        registry = _make_registry(tmp_path, bash_defn)
        # Sanity: bash + noop resolve.
        assert isinstance(registry.get("kolsch/nodes/bash", node_name="n"), BashRunner)
        assert isinstance(registry.get("kolsch/nodes/noop", node_name="n"), NoopRunner)
        # Phase 3.4 Step 0 — ``user_input`` is now wired by
        # register_builtins (no provider deps; raises a typed pause
        # signal that the executor's worklist catches separately from
        # the failure path).
        assert isinstance(
            registry.get("kolsch/nodes/user_input", node_name="n"),
            UserInputRunner,
        )
        # Deferred types still raise — including ``agent`` when no
        # engine + session_store + config_builder triple is supplied,
        # and the file-IO trio when their respective ``*_defn`` kwargs
        # are absent (Phase 3.5 made registration conditional on the
        # caller threading a tool definition through). Phase 3.6
        # registers ``branch`` / ``filter`` / ``compute`` unconditionally
        # (pure Jinja, no provider deps); Phase 3.7 registers ``map``
        # unconditionally (also pure-Python, just needs the registry
        # back-reference for worker dispatch). ``workflow`` keeps its
        # gate — it needs the ``workflow_store`` + ``executor_factory``
        # pair which only the CLI bootstrap can supply.
        for name, expected_phase in (
            ("agent", "3.2"),
            ("read_file", "3.5"),
            ("write_file", "3.5"),
            ("list_files", "3.5"),
            ("workflow", "3.7"),
        ):
            with pytest.raises(NodeTypeNotImplemented) as exc_info:
                registry.get(f"kolsch/nodes/{name}", node_name="n")
            assert exc_info.value.phase == expected_phase


# ---------------------------------------------------------------------------
# Phase 3.2 §8.5.1 — AgentRunner registers when the engine triple is supplied
# ---------------------------------------------------------------------------


class TestAgentRunnerRegistration:
    def test_agent_runner_registers_when_triple_supplied(
        self, tmp_path: Path, bash_defn: Any
    ) -> None:
        """Phase 3.2 §8.5.1 + AC 3.1/3.3 — when ``engine`` +
        ``session_store`` + ``config_builder`` (and ``catalog``) are
        all provided, ``register_builtins`` installs an
        :class:`AgentRunner` against ``kolsch/nodes/agent`` and the
        slot stops raising :class:`NodeTypeNotImplemented`.
        """
        workspace = _make_workspace(tmp_path)
        platform_dir = workspace / ".platform"
        platform_dir.mkdir(exist_ok=True)
        # SqliteBackend.initialize() is async but no DB I/O happens
        # unless a session is created. The .get() check below just
        # walks the registry, so a sync construct is enough.
        store = SessionStore(
            root=workspace, backend=SqliteBackend(platform_dir / "entities.db")
        )

        async def _builder(_ref: str) -> ResolvedAgentConfig:
            raise AssertionError("unused")

        registry = RunnerRegistry()
        register_builtins(
            registry,
            bash_defn=bash_defn,
            authz=RuleBasedAuthorizationProvider(
                config={
                    "rules": [{"who": ".*", "what": ".*", "effect": "allow"}],
                },
            ),
            secrets=DotenvSecretsProvider(root=workspace, config={"path": ".env"}),
            sandbox=NativeSandboxProvider(config={}),
            catalog=AssetCatalog(),
            engine=cast("AgentEngine", object()),
            session_store=store,
            config_builder=_builder,
        )
        resolved = registry.get("kolsch/nodes/agent", node_name="classify")
        assert isinstance(resolved, AgentRunner)

    def test_agent_slot_still_raises_when_engine_missing(
        self, tmp_path: Path, bash_defn: Any
    ) -> None:
        """Triple is intentionally all-or-nothing — passing only some
        of the kwargs (e.g. forgot ``config_builder``) leaves the
        ``kolsch/nodes/agent`` slot DEFERRED so the gap surfaces
        loudly instead of installing a half-built runner.
        """
        workspace = _make_workspace(tmp_path)
        platform_dir = workspace / ".platform"
        platform_dir.mkdir(exist_ok=True)
        store = SessionStore(
            root=workspace, backend=SqliteBackend(platform_dir / "entities.db")
        )

        registry = RunnerRegistry()
        register_builtins(
            registry,
            bash_defn=bash_defn,
            authz=RuleBasedAuthorizationProvider(
                config={
                    "rules": [{"who": ".*", "what": ".*", "effect": "allow"}],
                },
            ),
            secrets=DotenvSecretsProvider(root=workspace, config={"path": ".env"}),
            sandbox=NativeSandboxProvider(config={}),
            catalog=AssetCatalog(),
            session_store=store,
            # engine= and config_builder= deliberately OMITTED
        )
        with pytest.raises(NodeTypeNotImplemented) as exc_info:
            registry.get("kolsch/nodes/agent", node_name="n")
        assert exc_info.value.phase == "3.2"
