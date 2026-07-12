"""Golden workflow-run trace tests — Phase 3.1 Step 6.

Each fixture under :func:`_FIXTURES` runs a small workflow scenario
to completion, captures the JSONL produced by the executor, and
asserts semantic equality with a stored golden under
``tests/golden/runs/``. Volatile fields are canonicalised before
comparison so a fresh test run never re-emits a churning diff.

Canonical volatile-field substitution table — delegated to the
shared :class:`tests.golden._helpers.Canonicaliser` so workflow
goldens and session goldens use the same convention:

    ISO-8601 ts          → "<TS>"
    <prefix>_<uuid>      → "<prefix>_001"…    per-prefix 3-digit counter

Prefixes covered: ``ev|ses|th|cl|mem|wrk|tr|inv|tc`` (every
UUID4-backed id minted in the runtime). ``ag_`` ids are user-named
and stable, so they pass through verbatim.

The sentinels increment in **order of first occurrence in the JSONL
text** so two events sharing the same id resolve to the same sentinel
even across distant lines. The substitution table is intentionally
principled — no per-fixture ad-hoc replacements.

Updating goldens: run a fixture, copy the printed-on-failure
``actual`` block over the golden file, re-run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from tests.golden._helpers import Canonicaliser, canonicalize_text

if TYPE_CHECKING:
    from collections.abc import Callable

from assets.catalog import AssetCatalog
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run_store import WorkflowRunStore

# ---------------------------------------------------------------------------
# Canonicaliser
# ---------------------------------------------------------------------------


def canonicalize_jsonl(text: str) -> str:
    """Thin shim — delegates to the shared
    :func:`tests.golden._helpers.canonicalize_text`. Kept as a named
    surface so the fixture code reads naturally, and so an in-test
    monkeypatch on the workflow-specific name doesn't reach across
    into Phase 1 goldens."""
    return canonicalize_text(text)


# ---------------------------------------------------------------------------
# Golden update helper
# ---------------------------------------------------------------------------


def _assert_or_update_golden(
    golden_path: Path, actual: str, fixture_name: str,
) -> None:
    """Compare ``actual`` to ``golden_path`` content; write if
    ``UPDATE_GOLDEN=1`` is set. Consolidates the four assertion
    sites so future schema-additive changes (like bug-036c adding
    ``node_config_preview`` to ``NODE_STARTED.payload``) can
    regenerate all goldens with one env var instead of hand-editing
    each file.
    """
    if os.getenv("UPDATE_GOLDEN"):
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(actual, encoding="utf-8")
        return
    if not golden_path.exists():
        msg = (
            f"Missing golden for fixture {fixture_name!r}.\n"
            f"To create it, run with UPDATE_GOLDEN=1 or write to "
            f"{golden_path}:\n\n{actual}"
        )
        raise AssertionError(msg)
    expected = golden_path.read_text(encoding="utf-8")
    assert actual == expected, (
        f"Golden mismatch for {fixture_name!r}.\n"
        f"--- expected ({golden_path}) ---\n{expected}"
        f"--- actual ---\n{actual}"
    )


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


class _StaticRunner:
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


class _BashStubRunner:
    """Mirrors :class:`workflows.builtins.bash.BashRunner`'s non-zero
    exit raise so branching golden fixtures exercise the executor's
    :class:`workflows.errors.BashNonZeroExit` catch arm without
    standing up the tool-dispatch pipeline.
    """

    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    async def run(
        self,
        *,
        node_name: str,
        node_def: Any,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: Any,
    ) -> dict[str, Any]:
        from workflows.errors import BashNonZeroExit

        _ = (node_def, bound_inputs, state, ctx)
        captured = dict(self._captured)
        if captured.get("exit_code", 0) != 0:
            raise BashNonZeroExit(node=node_name, captured=captured)
        return captured


class _CancelOnFireRunner:
    """Drops ``cancel.flag`` at end of run — simulates an operator
    cancellation racing the executor's node-boundary poll."""

    def __init__(self, cancel_path: Path) -> None:
        self._cancel_path = cancel_path

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
        self._cancel_path.write_text("", encoding="utf-8")
        return {"stdout": "interrupted\n", "stderr": "", "exit_code": 0}


def _build_linear_bash_pipeline(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Three nodes piped sequentially: bash → bash → bash.

    The ``bash`` runner is mocked (no actual subprocess) so the golden
    is reproducible across hosts.
    """
    workflow = Workflow.model_validate(
        {
            "nodes": {
                "first": {
                    "type": "kolsch/nodes/bash",
                    "command": "echo first",
                },
                "second": {
                    "type": "kolsch/nodes/bash",
                    "command": "echo second",
                },
                "third": {
                    "type": "kolsch/nodes/bash",
                    "command": "echo third",
                },
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
        _StaticRunner({"stdout": "ok\n", "stderr": "", "exit_code": 0}),
    )
    _ = tmp_path
    return workflow, registry, {}


def _build_authored_python_basemodel(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """One authored-Python ``@node`` returning a Pydantic ``BaseModel``.

    Uses a mock runner that mimics the catalog-runner output shape —
    keeping the golden independent of the on-disk module path the
    real catalog runner would import. The shape under test is the
    EVENT stream, not the importlib pipeline (which has its own
    coverage in :mod:`tests.component.test_workflow_catalog_node_runner`).
    """
    workflow = Workflow.model_validate(
        {
            "nodes": {
                "echo": {"type": "demo/nodes/echo"},
            },
            "graph": {"edges": []},
        }
    )
    registry = RunnerRegistry()
    # Return shape matches what CatalogNodeRunner._coerce_return
    # produces for a BaseModel-returning @node — see master spec §5.4.
    registry.register(
        "demo/nodes/echo",
        _StaticRunner({"message": "Hello, world!"}),
    )
    _ = tmp_path
    # The validator's ``catalog-node-defs`` pass requires the ref to
    # resolve in the catalog. Marking the entry ``builtin: True``
    # short-circuits the importlib introspection so the mock runner
    # injected above carries the whole load.
    from assets.catalog import AssetCatalog, AssetEntry

    catalog = AssetCatalog(
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
    return workflow, registry, {"catalog": catalog}


def _build_cancel_mid_run(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Two-node bash workflow; first node trips ``cancel.flag`` so the
    executor aborts at the boundary before ``second`` fires.
    """
    workflow = Workflow.model_validate(
        {
            "nodes": {
                "first": {
                    "type": "kolsch/nodes/bash",
                    "command": "echo first",
                },
                "second": {
                    "type": "kolsch/nodes/bash",
                    "command": "echo second",
                },
            },
            "graph": {"edges": [{"from": "first", "to": "second"}]},
        }
    )
    registry = RunnerRegistry()
    cancel_path = tmp_path / "runs_cancel_marker"
    registry.register(
        "kolsch/nodes/bash",
        _CancelOnFireRunner(cancel_path=cancel_path),
    )
    return workflow, registry, {"cancel_path": cancel_path}


def _build_branching_bash(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Phase 3.3 — bash exit-code branching.

    The ``check`` node exits non-zero so the ``branch: false`` edge
    fires (``on_failure`` runs) and the ``branch: true`` arm
    (``on_success``) is pruned, emitting ``workflow.node.pruned``.
    The golden pins the branch_value payload + the pruned event's
    upstream_source chain.
    """
    workflow = Workflow.model_validate(
        {
            "nodes": {
                "check": {
                    "type": "kolsch/nodes/bash",
                    "command": "false",
                },
                "on_success": {
                    "type": "kolsch/nodes/bash",
                    "command": "echo ok",
                },
                "on_failure": {
                    "type": "kolsch/nodes/bash",
                    "command": "echo recovered",
                },
            },
            "graph": {
                "edges": [
                    {"from": "check", "to": "on_success", "branch": True},
                    {"from": "check", "to": "on_failure", "branch": False},
                ]
            },
        }
    )
    registry = RunnerRegistry()

    # ``check`` exits non-zero; ``on_success`` / ``on_failure`` use a
    # success-exit stub. The registry maps type→runner, so a single
    # bash runner can't differentiate by node name. We use the typed
    # protocol's ``node_name`` parameter to route exit codes.
    class _NodeKeyedBash:
        def __init__(self, by_node: dict[str, dict[str, Any]]) -> None:
            self._by_node = by_node

        async def run(
            self,
            *,
            node_name: str,
            node_def: Any,
            bound_inputs: dict[str, Any],
            state: dict[str, Any],
            ctx: Any,
        ) -> dict[str, Any]:
            from workflows.errors import BashNonZeroExit

            _ = (node_def, bound_inputs, state, ctx)
            captured = dict(self._by_node[node_name])
            if captured.get("exit_code", 0) != 0:
                raise BashNonZeroExit(node=node_name, captured=captured)
            return captured

    registry.register(
        "kolsch/nodes/bash",
        _NodeKeyedBash(
            {
                "check": {"stdout": "", "stderr": "", "exit_code": 1},
                "on_success": {
                    "stdout": "ok\n",
                    "stderr": "",
                    "exit_code": 0,
                },
                "on_failure": {
                    "stdout": "recovered\n",
                    "stderr": "",
                    "exit_code": 0,
                },
            }
        ),
    )
    _ = tmp_path
    return workflow, registry, {}


def _build_branching_python(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Phase 3.3 — authored Python ``tuple[bool, BaseModel]`` branch.

    Writes a real ``classify`` module to disk so the validator's
    ``_is_branchable_catalog_node`` introspection sees the
    ``tuple[bool, BaseModel]`` return annotation and admits the
    ``branch:`` edges. Inside the executor, a static runner intercepts
    the type ref and returns the canonical capture shape
    ``{"value": True, "category": "urgent", "score": 0.95}`` so the
    JSONL stays reproducible across hosts (no importlib roundtrip).
    """
    import textwrap

    nodes_dir = tmp_path / "bundles" / "demo" / "nodes"
    nodes_dir.mkdir(parents=True, exist_ok=True)
    classify_path = nodes_dir / "classify.py"
    classify_path.write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            from pydantic import BaseModel

            from workflows import NodeContext, node


            class ClassifyInputs(BaseModel):
                text: str = ""


            class ClassifyOutputs(BaseModel):
                category: str
                score: float


            @node(label="Classify")
            async def run(
                inputs: ClassifyInputs, ctx: NodeContext
            ) -> tuple[bool, ClassifyOutputs]:
                _ = (inputs, ctx)
                return True, ClassifyOutputs(category="urgent", score=0.95)
            """
        ),
        encoding="utf-8",
    )
    workflow = Workflow.model_validate(
        {
            "nodes": {
                "classify": {"type": "demo/nodes/classify"},
                "on_urgent": {"type": "kolsch/nodes/noop"},
                "on_normal": {"type": "kolsch/nodes/noop"},
            },
            "graph": {
                "edges": [
                    {
                        "from": "classify",
                        "to": "on_urgent",
                        "branch": True,
                    },
                    {
                        "from": "classify",
                        "to": "on_normal",
                        "branch": False,
                    },
                ]
            },
        }
    )
    registry = RunnerRegistry()
    registry.register(
        "demo/nodes/classify",
        _StaticRunner({"value": True, "category": "urgent", "score": 0.95}),
    )
    registry.register("kolsch/nodes/noop", _StaticRunner({}))
    from assets.catalog import AssetEntry

    catalog = AssetCatalog(
        assets={
            "demo/nodes/classify": AssetEntry(
                ref="demo/nodes/classify",
                asset_type="node",
                bundle="demo",
                path=str(classify_path),
                metadata={},
            )
        }
    )
    return workflow, registry, {"catalog": catalog}


def _build_user_input_single_pause(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Phase 3.4 — single ``user_input`` pause + resume to terminal.

    A two-node ``ask → echo`` pipeline that pauses on ``ask``,
    resumes with ``{response: "world"}``, and continues to
    ``succeeded``. The downstream ``echo`` node consumes the
    resumed response via ``{{ nodes.ask.response }}`` so the
    golden's payload covers the cross-process replay path.
    """
    workflow = Workflow.model_validate(
        {
            "nodes": {
                "ask": {
                    "type": "kolsch/nodes/user_input",
                    "prompt": "What would you like to say?",
                },
                "echo": {"type": "demo/nodes/echo"},
            },
            "graph": {
                "edges": [
                    {
                        "from": "ask",
                        "to": "echo",
                        "binds": {
                            "message": "{{ nodes.ask.response }}",
                        },
                    }
                ]
            },
        }
    )
    from workflows.builtins.user_input import UserInputRunner

    registry = RunnerRegistry()
    registry.register("kolsch/nodes/user_input", UserInputRunner())
    # ``demo/nodes/echo`` mirrors the catalog-runner's BaseModel-
    # returning shape — see :func:`_build_authored_python_basemodel`.
    registry.register(
        "demo/nodes/echo",
        _StaticRunner({"message": "world"}),
    )
    from assets.catalog import AssetEntry

    catalog = AssetCatalog(
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
    _ = tmp_path
    return (
        workflow,
        registry,
        {
            "catalog": catalog,
            "resume_responses": [{"response": "world"}],
        },
    )


def _build_user_input_sequential_pauses(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Phase 3.4 — two sequential ``user_input`` pauses.

    ``ask1 → between → ask2 → done``. Each pause writes a fresh
    ``waiting_at`` sidecar; each resume completes the open
    invocation and re-enters the worklist. The final ``done`` node
    captures a combined payload.
    """
    workflow = Workflow.model_validate(
        {
            "nodes": {
                "ask1": {
                    "type": "kolsch/nodes/user_input",
                    "prompt": "First?",
                },
                "between": {"type": "demo/nodes/echo"},
                "ask2": {
                    "type": "kolsch/nodes/user_input",
                    "prompt": "Second?",
                },
                "done": {"type": "demo/nodes/echo"},
            },
            "graph": {
                "edges": [
                    {
                        "from": "ask1",
                        "to": "between",
                        "binds": {
                            "message": "{{ nodes.ask1.response }}",
                        },
                    },
                    {"from": "between", "to": "ask2"},
                    {
                        "from": "ask2",
                        "to": "done",
                        "binds": {
                            "message": (
                                "{{ nodes.ask1.response }}+{{ nodes.ask2.response }}"
                            ),
                        },
                    },
                ]
            },
        }
    )
    from workflows.builtins.user_input import UserInputRunner

    registry = RunnerRegistry()
    registry.register("kolsch/nodes/user_input", UserInputRunner())

    # Per-node-name echo runner so ``between`` and ``done`` surface
    # distinct captured payloads in the golden.
    class _NamedEchoRunner:
        def __init__(self, by_node: dict[str, dict[str, Any]]) -> None:
            self._by_node = by_node

        async def run(
            self,
            *,
            node_name: str,
            node_def: Any,
            bound_inputs: dict[str, Any],
            state: dict[str, Any],
            ctx: Any,
        ) -> dict[str, Any]:
            _ = (node_def, bound_inputs, state, ctx)
            return dict(self._by_node[node_name])

    registry.register(
        "demo/nodes/echo",
        _NamedEchoRunner(
            {
                "between": {"message": "alpha"},
                "done": {"message": "alpha+beta"},
            }
        ),
    )
    from assets.catalog import AssetEntry

    catalog = AssetCatalog(
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
    _ = tmp_path
    return (
        workflow,
        registry,
        {
            "catalog": catalog,
            "resume_responses": [
                {"response": "alpha"},
                {"response": "beta"},
            ],
        },
    )


def _build_user_input_rejected_resume(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Phase 3.4 — paused run with a rejected (bad-input) resume.

    The bad resume raises ``WorkflowInputInvalid`` BEFORE any
    events are appended; the golden therefore matches the paused
    run's JSONL byte-for-byte (canonicalised), proving the
    rejection is non-mutating.
    """
    workflow = Workflow.model_validate(
        {
            "nodes": {
                "ask": {
                    "type": "kolsch/nodes/user_input",
                    "prompt": "?",
                }
            }
        }
    )
    from workflows.builtins.user_input import UserInputRunner

    registry = RunnerRegistry()
    registry.register("kolsch/nodes/user_input", UserInputRunner())
    _ = tmp_path
    return (
        workflow,
        registry,
        {
            # Bad resume — ``WorkflowInputInvalid`` is raised; the
            # harness asserts the JSONL is unchanged afterward.
            "resume_responses": [{"wrong_key": "x"}],
            "expect_resume_exception": "WorkflowInputInvalid",
            # The run stays paused — terminal status is
            # ``waiting_for_input``.
            "final_status_override": "waiting_for_input",
        },
    )


def _build_state_evolves(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Phase 3.5 §4.3.1 — three sequential writes to ``attempts``.

    The canonical "evolving variable" pattern. Three
    ``workflow.state.mutated`` events land between started/completed
    pairs with values 0, 1, 2; the final completion event's
    ``parent_event_id`` chains to the LAST state-mutated event per
    master spec §11.3.
    """
    from workflows.builtins.set_state import SetStateRunner

    workflow = Workflow.model_validate(
        {
            "nodes": {
                "init": {
                    "type": "kolsch/nodes/set_state",
                    "keys": {"attempts": 0},
                },
                "bump1": {
                    "type": "kolsch/nodes/set_state",
                    "keys": {"attempts": "{{ state.attempts + 1 }}"},
                },
                "bump2": {
                    "type": "kolsch/nodes/set_state",
                    "keys": {"attempts": "{{ state.attempts + 1 }}"},
                },
            },
            "graph": {
                "edges": [
                    {"from": "init", "to": "bump1"},
                    {"from": "bump1", "to": "bump2"},
                ]
            },
        }
    )
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/set_state", SetStateRunner())
    _ = tmp_path
    return workflow, registry, {}


def _build_delete_state_idempotent(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Phase 3.5 §4.3.2 — ``delete_state`` of an absent key.

    Silent on absent state; event STILL emitted (forensic + replay
    parity). The golden pins the ``{key, op: "delete", value: null}``
    payload shape.
    """
    from workflows.builtins.delete_state import (
        DeleteStateRunner,
    )

    workflow = Workflow.model_validate(
        {
            "nodes": {
                "drop": {
                    "type": "kolsch/nodes/delete_state",
                    "keys": ["foo"],
                },
            }
        }
    )
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/delete_state", DeleteStateRunner())
    _ = tmp_path
    return workflow, registry, {}


def _build_file_roundtrip(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Phase 3.5 §4.2 — write → read → list against a real workspace.

    The runner registry threads the real file-IO tool factories so
    the JSONL exercises the full dispatch pipeline (PathPolicy +
    atomic write + read-back + listing). The ``list`` node uses a
    pattern that matches only the written file so the golden's
    ``entries`` payload stays deterministic across hosts (no
    leakage from sibling tmp dirs).
    """
    from files import PathPolicy
    from providers.authz import (
        RuleBasedAuthorizationProvider,
    )
    from providers.sandbox import NativeSandboxProvider
    from providers.secrets import DotenvSecretsProvider
    from tools.builtins import (
        make_bash,
        make_list_files,
        make_read_file,
        make_write_file,
    )
    from tools.decorator import get_tool_definition
    from workflows.builtins import register_builtins

    workflow = Workflow.model_validate(
        {
            "nodes": {
                "write": {
                    "type": "kolsch/nodes/write_file",
                    "path": "out.txt",
                    "content": "hello",
                },
                "read": {
                    "type": "kolsch/nodes/read_file",
                    "path": "out.txt",
                },
                "list": {
                    "type": "kolsch/nodes/list_files",
                    "path": ".",
                    "pattern": "out.txt",
                },
            },
            "graph": {
                "edges": [
                    {"from": "write", "to": "read"},
                    {"from": "read", "to": "list"},
                ]
            },
        }
    )
    policy = PathPolicy(allowed_paths=["."])
    catalog = AssetCatalog()
    registry = RunnerRegistry()
    register_builtins(
        registry,
        bash_defn=get_tool_definition(make_bash(policy=policy, root=tmp_path)),
        authz=RuleBasedAuthorizationProvider(
            config={"rules": [{"who": ".*", "what": ".*", "effect": "allow"}]},
            catalog=catalog,
        ),
        secrets=DotenvSecretsProvider(root=tmp_path, config={"path": ".env"}),
        sandbox=NativeSandboxProvider(config={}),
        catalog=catalog,
        read_file_defn=get_tool_definition(
            make_read_file(policy=policy, root=tmp_path)
        ),
        write_file_defn=get_tool_definition(
            make_write_file(policy=policy, root=tmp_path)
        ),
        list_files_defn=get_tool_definition(
            make_list_files(policy=policy, root=tmp_path)
        ),
    )
    return workflow, registry, {}


def _build_file_access_denied(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Phase 3.5 §4.2 + §12 — denied path surfaces as
    ``WorkflowFileAccessDenied``; run aborts via the existing failure
    path. The golden pins ``error_class`` + run.failed terminal.
    """
    from files import PathPolicy
    from providers.authz import (
        RuleBasedAuthorizationProvider,
    )
    from providers.sandbox import NativeSandboxProvider
    from providers.secrets import DotenvSecretsProvider
    from tools.builtins import (
        make_bash,
        make_read_file,
    )
    from tools.decorator import get_tool_definition
    from workflows.builtins import register_builtins

    workflow = Workflow.model_validate(
        {
            "nodes": {
                "read": {
                    "type": "kolsch/nodes/read_file",
                    "path": "/etc/passwd",
                },
            }
        }
    )
    # ``allowed_paths`` restricted to the workspace data subdir; the
    # absolute path resolves outside → FileAccessDenied → remapped to
    # WorkflowFileAccessDenied → run.failed.
    policy = PathPolicy(allowed_paths=["data/"])
    (tmp_path / "data").mkdir()
    catalog = AssetCatalog()
    registry = RunnerRegistry()
    register_builtins(
        registry,
        bash_defn=get_tool_definition(make_bash(policy=policy, root=tmp_path)),
        authz=RuleBasedAuthorizationProvider(
            config={"rules": [{"who": ".*", "what": ".*", "effect": "allow"}]},
            catalog=catalog,
        ),
        secrets=DotenvSecretsProvider(root=tmp_path, config={"path": ".env"}),
        sandbox=NativeSandboxProvider(config={}),
        catalog=catalog,
        read_file_defn=get_tool_definition(
            make_read_file(policy=policy, root=tmp_path)
        ),
    )
    return workflow, registry, {}


def _build_triage(
    tmp_path: Path,
    *,
    classify_label: str,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Phase 3.6 §6.0.1 — triage walkthrough.

    Builds the canonical ``classify → is_urgent → urgent/normal_handler``
    pipeline. The agent runner is a node-keyed static stub: ``classify``
    returns ``{text: classify_label}`` (which the workflow's edge bind
    pulls into ``inputs.label`` for the branch condition), and each
    responder returns a deterministic acknowledgement so the
    captured-output payload is stable across hosts.

    The ``branch:`` runner is exercised end-to-end —
    ``compute_branch_value`` reads its ``{value: bool}`` capture, the
    edge dispatcher routes the matching arm, and the other arm emits
    ``workflow.node.pruned`` via the Phase 3.3 cascade.
    """
    from workflows.builtins.branch import BranchRunner

    workflow = Workflow.model_validate(
        {
            "nodes": {
                "classify": {
                    "type": "kolsch/nodes/agent",
                    "agent": "kolsch/agents/classifier",
                },
                "is_urgent": {
                    "type": "kolsch/nodes/branch",
                    "condition": "{{ inputs.label == 'urgent' }}",
                },
                "urgent_handler": {
                    "type": "kolsch/nodes/agent",
                    "agent": "kolsch/agents/urgent_responder",
                },
                "normal_handler": {
                    "type": "kolsch/nodes/agent",
                    "agent": "kolsch/agents/normal_responder",
                },
            },
            "graph": {
                "edges": [
                    {
                        "from": "classify",
                        "to": "is_urgent",
                        "binds": {
                            "label": "{{ nodes.classify.text }}",
                        },
                    },
                    {
                        "from": "is_urgent",
                        "to": "urgent_handler",
                        "branch": True,
                    },
                    {
                        "from": "is_urgent",
                        "to": "normal_handler",
                        "branch": False,
                    },
                ]
            },
        }
    )

    class _NodeKeyedAgent:
        """Static agent runner keyed by node name.

        Mirrors the engine's single_shot wrapping shape
        (``{text: "..."}``) so the workflow's edge-bound
        ``{{ nodes.classify.text }}`` resolves the same way it would
        with a real :class:`AgentRunner`.
        """

        def __init__(self, by_node: dict[str, dict[str, Any]]) -> None:
            self._by_node = by_node

        async def run(
            self,
            *,
            node_name: str,
            node_def: Any,
            bound_inputs: dict[str, Any],
            state: dict[str, Any],
            ctx: Any,
        ) -> dict[str, Any]:
            _ = (node_def, bound_inputs, state, ctx)
            return dict(self._by_node[node_name])

    registry = RunnerRegistry()
    registry.register("kolsch/nodes/branch", BranchRunner())
    registry.register(
        "kolsch/nodes/agent",
        _NodeKeyedAgent(
            {
                "classify": {"text": classify_label},
                "urgent_handler": {"text": "Acked urgent ticket"},
                "normal_handler": {"text": "Acked normal ticket"},
            }
        ),
    )
    # Mark the agent refs as catalog entries so the validator's
    # ``catalog-node-defs`` pass accepts them. ``builtin=True``
    # short-circuits the importlib introspection; the static runner
    # carries the load.
    from assets.catalog import AssetEntry

    catalog = AssetCatalog(
        assets={
            ref: AssetEntry(
                ref=ref,
                asset_type="agent",
                bundle="kolsch",
                path="",
                metadata={"builtin": True},
            )
            for ref in (
                "kolsch/agents/classifier",
                "kolsch/agents/urgent_responder",
                "kolsch/agents/normal_responder",
            )
        }
    )
    _ = tmp_path
    return workflow, registry, {"catalog": catalog}


def _build_triage_urgent(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Triage with classifier returning ``urgent`` — true arm fires."""
    return _build_triage(tmp_path, classify_label="urgent")


def _build_triage_normal(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Triage with classifier returning ``normal`` — false arm fires."""
    return _build_triage(tmp_path, classify_label="normal")


def _build_state_resume(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Phase 3.5 — pause + resume preserves state via JSONL replay.

    Pipeline: ``stash`` (set_state attempts=5) → ``ask`` (pause) →
    ``echo`` (catalog node — captures bound ``{{ state.attempts }}``
    post-replay). Exercises the
    ``_replay.py:workflow.state.mutated`` arm + the ordering bug
    fixed alongside Step 3 (resume now initialises ``_state`` BEFORE
    replay rather than overwriting after).
    """
    from workflows.builtins.set_state import SetStateRunner
    from workflows.builtins.user_input import UserInputRunner

    workflow = Workflow.model_validate(
        {
            "nodes": {
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
                    {"from": "stash", "to": "ask"},
                    {
                        "from": "ask",
                        "to": "echo",
                        "binds": {"message": "{{ state.attempts }}"},
                    },
                ]
            },
        }
    )
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/set_state", SetStateRunner())
    registry.register("kolsch/nodes/user_input", UserInputRunner())
    # Static echo runner — mirrors what CatalogNodeRunner would
    # produce. ``message`` lands in bound_inputs and is echoed back;
    # asserting it == 5 in the golden proves state replay restored
    # ``state.attempts`` before the bind rendered.
    registry.register(
        "demo/nodes/echo",
        _StaticRunner({"message": 5}),
    )
    from assets.catalog import AssetEntry

    catalog = AssetCatalog(
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
    _ = tmp_path
    return (
        workflow,
        registry,
        {
            "catalog": catalog,
            "resume_responses": [{"response": "ok"}],
        },
    )


def _build_branching_multi_input(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Phase 3.3 — multi-input partial fan-in. ``branch_src`` branches
    true so the ``branch: false`` edge to ``receiver`` is pruned;
    ``fan_in`` always fires and contributes ``from_b`` to the
    receiver's binds. ``receiver`` fires with the surviving binds
    only — proving partial fan-in semantics on the JSONL surface.
    """
    workflow = Workflow.model_validate(
        {
            "nodes": {
                "branch_src": {
                    "type": "kolsch/nodes/bash",
                    "command": "true",
                },
                "fan_in": {"type": "kolsch/nodes/noop"},
                "receiver": {
                    "type": "kolsch/nodes/agent",
                    "agent": "demo/agents/x",
                },
            },
            "graph": {
                "edges": [
                    {
                        "from": "branch_src",
                        "to": "receiver",
                        "branch": False,
                        "binds": {"from_pruned": "alpha"},
                    },
                    {
                        "from": "branch_src",
                        "to": "fan_in",
                        "branch": True,
                    },
                    {
                        "from": "fan_in",
                        "to": "receiver",
                        "binds": {"from_b": "beta"},
                    },
                ]
            },
        }
    )
    registry = RunnerRegistry()
    registry.register(
        "kolsch/nodes/bash",
        _BashStubRunner({"stdout": "", "stderr": "", "exit_code": 0}),
    )
    registry.register("kolsch/nodes/noop", _StaticRunner({}))
    registry.register("kolsch/nodes/agent", _StaticRunner({}))
    _ = tmp_path
    return workflow, registry, {}


# ---------------------------------------------------------------------------
# Phase 3.7 — composition goldens (sub-workflow + map).
#
# Each fixture below writes the supporting workflows to disk under
# ``tmp_path/bundles/`` and threads an :class:`AssetCatalog` with the
# child entries through ``extras["catalog"]`` so the existing harness
# uses it when constructing the executor + :class:`WorkflowSubRunner`.
# Goldens capture the ROOT run's JSONL — the cross-trace correlation
# is verifiable because the child's run_id appears verbatim in the
# parent's ``child_run_id`` payload field.
# ---------------------------------------------------------------------------


def _ext_compose_write_workflow(path: Path, content: str) -> None:
    """Helper — write a sibling workflow YAML to disk."""
    import textwrap as _tw

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_tw.dedent(content).lstrip(), encoding="utf-8")


def _ext_compose_catalog(*, tmp_path: Path, refs: dict[str, str]) -> AssetCatalog:
    """Build an AssetCatalog pointing at the on-disk workflow YAMLs
    declared in ``refs`` (``{ref: yaml_contents}``)."""
    from assets.catalog import AssetCatalog, AssetEntry

    assets: dict[str, AssetEntry] = {}
    for ref, content in refs.items():
        bundle, _kind, name = ref.split("/")
        path = tmp_path / "bundles" / bundle / "workflows" / f"{name}.yaml"
        _ext_compose_write_workflow(path, content)
        assets[ref] = AssetEntry(
            ref=ref,
            asset_type="workflow",
            bundle=bundle,
            path=str(path),
        )
    return AssetCatalog(assets=assets)


def _ext_compose_registry(
    *, catalog: AssetCatalog, store: WorkflowRunStore
) -> RunnerRegistry:
    """Build a registry pre-wired for sub-workflow + map dispatch."""
    from workflows.builtins.compute import ComputeRunner
    from workflows.builtins.map import MapRunner
    from workflows.builtins.noop import NoopRunner
    from workflows.builtins.workflow_sub import (
        WorkflowSubRunner,
    )

    registry = RunnerRegistry()
    registry.register("kolsch/nodes/noop", NoopRunner())
    registry.register("kolsch/nodes/compute", ComputeRunner())
    registry.register("kolsch/nodes/map", MapRunner(registry=registry))

    def _factory(**kwargs: Any) -> IterativeExecutor:
        return IterativeExecutor(**kwargs)

    registry.register(
        "kolsch/nodes/workflow",
        WorkflowSubRunner(
            catalog=catalog,
            store=store,
            executor_factory=_factory,
            registry=registry,
        ),
    )
    return registry


def _build_parallel_map_demo(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Phase 3.10 §16.3 — ``parallel: true`` map over a 3-element list.

    Locks the iteration interleaving in the JSONL under the
    concurrent dispatch path: every per-iteration ``started`` event
    appears BEFORE any per-iteration ``completed`` event because the
    compute worker has no internal awaits and the event-loop's
    cooperative scheduling pumps each task to its first ``await
    ctx.emit_workflow_event`` before resuming the next. Also locks
    the index-ordered ``results`` payload on the map node's own
    ``workflow.node.completed`` event regardless of completion
    interleaving (master spec §16.3 contract).
    """
    workflow = Workflow.model_validate(
        {
            "nodes": {
                "echo_one": {
                    "type": "kolsch/nodes/compute",
                    "value": "{{ inputs.item }}",
                },
                "fan_out": {
                    "type": "kolsch/nodes/map",
                    "apply": "echo_one",
                    "over": [1, 2, 3],
                    "item_as": "item",
                    "parallel": True,
                    "concurrency": 3,
                },
            },
            "graph": {"edges": []},
        }
    )
    store = WorkflowRunStore(tmp_path)
    catalog = _ext_compose_catalog(tmp_path=tmp_path, refs={})
    registry = _ext_compose_registry(catalog=catalog, store=store)
    return workflow, registry, {"catalog": catalog}


def _build_composition_map(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Phase 3.7 — ``map`` over a 3-element list with a compute worker.

    Locks: per-iteration ``workflow.node.started`` / ``_completed``
    payload shape (``parent_invocation_id`` + ``iteration_index`` +
    ``iteration_total``), the map node's collected
    ``{results: [...]}`` output, and the worker-only-node-skip
    behavior in ``_compute_topology`` (the worker doesn't fire as a
    standalone topo root).
    """
    workflow = Workflow.model_validate(
        {
            "nodes": {
                "echo_one": {
                    "type": "kolsch/nodes/compute",
                    "value": "{{ inputs.item }}",
                },
                "fan_out": {
                    "type": "kolsch/nodes/map",
                    "apply": "echo_one",
                    "over": [1, 2, 3],
                    "item_as": "item",
                },
            },
            "graph": {"edges": []},
        }
    )
    store = WorkflowRunStore(tmp_path)
    catalog = _ext_compose_catalog(tmp_path=tmp_path, refs={})
    registry = _ext_compose_registry(catalog=catalog, store=store)
    return workflow, registry, {"catalog": catalog}


def _build_composition_nesting(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Phase 3.7 — parent calls a one-node child.

    Locks: ``workflow.node.called_workflow`` + ``_completed`` event
    pair shape, the ``child_run_id`` payload extension on the parent's
    ``workflow.node.completed``, and ``trace_id`` inheritance across
    the parent / child JSONLs (the canonicaliser maps both to the
    same sentinel because they share the literal id string).
    """
    workflow = Workflow.model_validate(
        {
            "nodes": {
                "sub": {
                    "type": "kolsch/nodes/workflow",
                    "ref": "demo/workflows/child",
                },
            },
        }
    )
    child_yaml = """
    nodes:
      only:
        type: kolsch/nodes/compute
        value: "leaf-output"
    graph:
      edges: []
    """
    store = WorkflowRunStore(tmp_path)
    catalog = _ext_compose_catalog(
        tmp_path=tmp_path,
        refs={"demo/workflows/child": child_yaml},
    )
    registry = _ext_compose_registry(catalog=catalog, store=store)
    return workflow, registry, {"catalog": catalog}


def _build_composition_cycle(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Phase 3.7 — a workflow that calls itself.

    Locks: the cycle-detection envelope's wire shape — the parent's
    ``workflow.node.failed`` payload's ``error_class``
    (``WorkflowCallCycle``) and message including the chain.
    Detection fires BEFORE ``called_workflow`` is emitted (the runner
    short-circuits early), so the JSONL stays compact:
    ``run.started → node.started(loop) → node.failed(loop) →
    run.failed``.

    The cycle check uses the executor's auto-included call_chain
    entry (``self._call_chain = (run.workflow_ref,)`` for root runs).
    The harness sets ``workflow_ref="demo/workflows/x"`` so the
    workflow node's ``ref:`` must match for the cycle to fire.
    """
    workflow = Workflow.model_validate(
        {
            "nodes": {
                "loop": {
                    "type": "kolsch/nodes/workflow",
                    "ref": "demo/workflows/x",
                },
            },
        }
    )
    store = WorkflowRunStore(tmp_path)
    catalog = _ext_compose_catalog(tmp_path=tmp_path, refs={})
    registry = _ext_compose_registry(catalog=catalog, store=store)
    return workflow, registry, {"catalog": catalog}


def _build_composition_depth_exceeded(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Phase 3.7 — chain of 17 workflows hits the depth-16 cap.

    The deepest nested-executor construction raises
    ``WorkflowExtendsDepthExceeded(depth=17, limit=16)``. The root's
    JSONL captures the failure CASCADE — each intermediate parent
    sees its child fail and propagates as ``ChildWorkflowFailed``.
    The JSONL spans 17 ``called_workflow`` markers + 17
    ``called_workflow_completed`` (failed) markers, plus the
    nested node.started / node.failed pairs. Compact enough for a
    golden, and locks the cross-level failure envelope shape.
    """
    depth = 17
    refs: dict[str, str] = {}
    for i in range(depth + 1):
        ref = f"demo/workflows/w{i}"
        if i < depth:
            content = f"""
                nodes:
                  next:
                    type: kolsch/nodes/workflow
                    ref: demo/workflows/w{i + 1}
                """
        else:
            content = """
            nodes:
              done:
                type: kolsch/nodes/compute
                value: "leaf"
            graph:
              edges: []
            """
        refs[ref] = content
    # The entry workflow IS w0 — but the harness's fixed
    # ``workflow_ref="demo/workflows/x"`` means the executor's
    # auto-included call_chain is ``("demo/workflows/x",)``, not
    # ``("demo/workflows/w0",)``. The depth cap fires regardless of
    # the chain's first entry because each sub-workflow appends a new
    # ref; what we lock is the ENVELOPE shape, not the specific chain
    # contents. The entry workflow IS w0's content though.
    entry_yaml = refs["demo/workflows/w0"]
    workflow = Workflow.model_validate(
        {
            "nodes": {
                "next": {
                    "type": "kolsch/nodes/workflow",
                    "ref": "demo/workflows/w1",
                },
            },
        }
    )
    _ = entry_yaml
    # Drop w0 from the catalog (it's the in-memory entry) and keep
    # w1..w17 on disk.
    refs.pop("demo/workflows/w0")
    store = WorkflowRunStore(tmp_path)
    catalog = _ext_compose_catalog(tmp_path=tmp_path, refs=refs)
    registry = _ext_compose_registry(catalog=catalog, store=store)
    return workflow, registry, {"catalog": catalog}


_FIXTURES: tuple[
    tuple[
        str,
        Callable[[Path], tuple[Workflow, RunnerRegistry, dict[str, Any]]],
        str,
    ],
    ...,
] = (
    ("linear-bash-pipeline", _build_linear_bash_pipeline, "succeeded"),
    (
        "authored-python-basemodel",
        _build_authored_python_basemodel,
        "succeeded",
    ),
    ("cancel-mid-run", _build_cancel_mid_run, "aborted"),
    ("branching-bash", _build_branching_bash, "succeeded"),
    ("branching-python", _build_branching_python, "succeeded"),
    (
        "branching-multi-input",
        _build_branching_multi_input,
        "succeeded",
    ),
    # Phase 3.5 — state + file-IO goldens.
    ("state-evolves", _build_state_evolves, "succeeded"),
    (
        "delete-state-idempotent",
        _build_delete_state_idempotent,
        "succeeded",
    ),
    ("file-roundtrip", _build_file_roundtrip, "succeeded"),
    ("file-access-denied", _build_file_access_denied, "failed"),
    # Phase 3.6 — triage walkthrough (both branch arms).
    ("triage-urgent", _build_triage_urgent, "succeeded"),
    ("triage-normal", _build_triage_normal, "succeeded"),
    # Phase 3.7 — composition (sub-workflow + map).
    ("composition-map", _build_composition_map, "succeeded"),
    ("composition-nesting", _build_composition_nesting, "succeeded"),
    ("composition-cycle", _build_composition_cycle, "failed"),
    (
        "composition-depth-exceeded",
        _build_composition_depth_exceeded,
        "failed",
    ),
    # Phase 3.10 — parallel map iteration golden.
    ("parallel-map-demo", _build_parallel_map_demo, "succeeded"),
)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


_GOLDEN_DIR = Path(__file__).parent / "runs"


@pytest.mark.parametrize(
    ("fixture_name", "builder", "expected_status"),
    _FIXTURES,
    ids=[name for name, *_ in _FIXTURES],
)
async def test_workflow_run_matches_golden(
    fixture_name: str,
    builder: Callable[[Path], tuple[Workflow, RunnerRegistry, dict[str, Any]]],
    expected_status: str,
    tmp_path: Path,
) -> None:
    """Run the fixture; canonicalize; diff against the golden file.

    To accept an intentional change to a golden, copy the
    ``actual`` block from the assertion's pytest output over the
    matching ``.jsonl`` under :data:`_GOLDEN_DIR` and re-run.
    """
    workflow, registry, extras = builder(tmp_path)

    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})

    # Special-case cancel-mid-run: redirect the runner's cancel path
    # to the run's actual cancel.flag location.
    if "cancel_path" in extras:
        runner_cancel_path = extras["cancel_path"]
        actual_cancel = store.directory(run.run_id).cancel_flag_path
        # Swap the runner's cancel path for the run's actual flag.
        for r in registry._runners.values():
            if isinstance(r, _CancelOnFireRunner):
                r._cancel_path = actual_cancel
                break
        _ = runner_cancel_path

    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=extras.get("catalog") or AssetCatalog(),
    )
    status = await ex.run()
    assert status.value == expected_status

    actual_raw = store.directory(run.run_id).events_path.read_text(encoding="utf-8")
    actual = canonicalize_jsonl(actual_raw)

    golden_path = _GOLDEN_DIR / f"{fixture_name}.jsonl"
    _assert_or_update_golden(golden_path, actual, fixture_name)


# ---------------------------------------------------------------------------
# Phase 3.4 — pause/resume goldens
# ---------------------------------------------------------------------------


_PAUSE_RESUME_FIXTURES: tuple[
    tuple[
        str,
        Callable[[Path], tuple[Workflow, RunnerRegistry, dict[str, Any]]],
        str,
    ],
    ...,
] = (
    (
        "user-input-single-pause.workflow",
        _build_user_input_single_pause,
        "succeeded",
    ),
    (
        "user-input-sequential-pauses.workflow",
        _build_user_input_sequential_pauses,
        "succeeded",
    ),
    (
        "user-input-rejected-resume.workflow",
        _build_user_input_rejected_resume,
        # The bad-input resume is rejected; the run stays paused.
        "waiting_for_input",
    ),
    # Phase 3.5 — state preserved across pause/resume via replay.
    ("state-resume.workflow", _build_state_resume, "succeeded"),
)


@pytest.mark.parametrize(
    ("fixture_name", "builder", "expected_status"),
    _PAUSE_RESUME_FIXTURES,
    ids=[name for name, *_ in _PAUSE_RESUME_FIXTURES],
)
async def test_workflow_pause_resume_matches_golden(
    fixture_name: str,
    builder: Callable[[Path], tuple[Workflow, RunnerRegistry, dict[str, Any]]],
    expected_status: str,
    tmp_path: Path,
) -> None:
    """Pause + resume each fixture; canonicalize; diff vs golden.

    The harness drives the cross-process lifecycle in a single
    process:

    1. Construct an executor for the initial run; ``await ex.run()``
       which pauses at the first ``user_input``.
    2. For each resume response in ``extras["resume_responses"]``,
       construct a FRESH executor pointed at the same run (mirrors
       the CLI's per-process model) and ``await fresh.resume(...)``.
    3. If ``extras["expect_resume_exception"]`` is set, assert the
       LAST resume raised that exception class and read the JSONL
       directly (no further resume).
    4. Canonicalise the JSONL and diff against the golden.

    Same canonicalisation table as the run-only goldens — the JSONL
    spans multiple process invocations but is one continuous file.
    """
    from workflows.errors import WorkflowInputInvalid

    exception_map: dict[str, type[Exception]] = {
        "WorkflowInputInvalid": WorkflowInputInvalid,
    }

    workflow, registry, extras = builder(tmp_path)
    catalog = extras.get("catalog") or AssetCatalog()
    resume_responses = list(extras.get("resume_responses") or [])
    expect_exc_name = extras.get("expect_resume_exception")
    final_status_override = extras.get("final_status_override")

    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})

    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=catalog,
    )
    initial_status = await ex.run()
    assert initial_status.value == "waiting_for_input", (
        f"fixture {fixture_name!r} expected initial pause; got {initial_status.value}"
    )

    last_status = initial_status
    for index, response in enumerate(resume_responses):
        is_last = index == len(resume_responses) - 1
        fresh_run = store.read(run.run_id)
        fresh_executor = IterativeExecutor(
            workflow=workflow,
            run=fresh_run,
            store=store,
            registry=registry,
            catalog=catalog,
        )
        if is_last and expect_exc_name is not None:
            exc_cls = exception_map[expect_exc_name]
            with pytest.raises(exc_cls):
                await fresh_executor.resume(response=response)
            # The rejection is non-mutating; status stays as it was.
            last_status = await _read_status(store, run.run_id)
            break
        last_status = await fresh_executor.resume(response=response)

    expected_value = final_status_override or expected_status
    assert last_status.value == expected_value, (
        f"fixture {fixture_name!r} expected terminal "
        f"{expected_value!r}; got {last_status.value!r}"
    )

    actual_raw = store.directory(run.run_id).events_path.read_text(encoding="utf-8")
    actual = canonicalize_jsonl(actual_raw)

    golden_path = _GOLDEN_DIR / f"{fixture_name}.jsonl"
    _assert_or_update_golden(golden_path, actual, fixture_name)


async def _read_status(store: WorkflowRunStore, run_id: Any) -> Any:
    """Re-read the run's current status from the store."""
    return store.read(run_id).status


# ---------------------------------------------------------------------------
# Phase 3.8 — fork goldens.
#
# Each fork fixture builds a parent workflow + registry plus enough
# metadata for the harness to:
#
#   1. Run the parent to ``succeeded``.
#   2. Locate the chosen fork-event in the parent's events.jsonl.
#   3. Mint a fresh forked :class:`WorkflowRun` carrying
#      :class:`ForkedFrom`; construct a NEW executor over it.
#   4. Invoke :meth:`IterativeExecutor.start_from_fork` with any
#      configured ``input_overrides``.
#
# Both the parent's JSONL and the fork's JSONL are then canonicalised
# through ONE :class:`Canonicaliser` instance so shared ids
# (notably ``trace_id``) resolve to identical sentinels in both
# golden files — the same paired-golden discipline Phase 3.2's
# workflow→agent cross-JSONL test relies on.
# ---------------------------------------------------------------------------


def _build_fork_pipeline(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """A → B → C pipeline built from ``kolsch/nodes/compute``.

    Each step's output schema is ``{value: <rendered>}``. The pipeline
    is the canonical fork-experimentation shape: forking at B's
    completion replays A + B and runs only C; forking at A's
    completion replays only A and runs B + C; forking at
    ``workflow.run.started`` re-runs everything.
    """
    from workflows.builtins.compute import ComputeRunner

    workflow = Workflow.model_validate(
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
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/compute", ComputeRunner())
    _ = tmp_path
    return workflow, registry, {}


def _build_fork_at_completed(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Fork at B's ``workflow.node.completed``. No overrides.

    The forked run replays A + B and runs only C (which sees B's
    replayed ``value=20`` via the edge bind).
    """
    workflow, registry, _ = _build_fork_pipeline(tmp_path)
    return (
        workflow,
        registry,
        {
            "fork_at_kind": "workflow.node.completed",
            "fork_at_node_name": "B",
            "input_overrides": {},
        },
    )


def _build_fork_at_started(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Fork at the parent's ``workflow.run.started`` event.

    The forked run inherits the parent's started baseline via replay
    but populates nothing in ``_completed_or_skipped``; the worklist
    re-fires every node.
    """
    workflow, registry, _ = _build_fork_pipeline(tmp_path)
    return (
        workflow,
        registry,
        {
            "fork_at_kind": "workflow.run.started",
            "fork_at_node_name": None,
            "input_overrides": {},
        },
    )


def _build_fork_with_input_override(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Fork at B's completed; override ``value`` to 99.

    C consumes ``v: {{ nodes.B.value }}`` so the bind renders to 99
    on the fork, vs the parent's 20. Locks the merge-override
    semantics on the forked JSONL.
    """
    workflow, registry, _ = _build_fork_pipeline(tmp_path)
    return (
        workflow,
        registry,
        {
            "fork_at_kind": "workflow.node.completed",
            "fork_at_node_name": "B",
            "input_overrides": {"value": 99},
        },
    )


def _build_fork_subworkflow_after(
    tmp_path: Path,
) -> tuple[Workflow, RunnerRegistry, dict[str, Any]]:
    """Parent: prep → sub (workflow-call) → wrap. Fork AFTER ``sub``.

    Spec §9.3 — forking at ``sub``'s ``workflow.node.completed``
    reuses the parent's captured child output. The forked run runs
    only ``wrap``; NO new child run is spawned. The golden locks the
    fork JSONL's absence of any ``workflow.node.called_workflow``
    event (replay handled the cross-JSONL link in-memory; the fork
    never re-emits the spawn marker).
    """
    workflow = Workflow.model_validate(
        {
            "nodes": {
                "prep": {
                    "type": "kolsch/nodes/compute",
                    "value": "prep-done",
                },
                "sub": {
                    "type": "kolsch/nodes/workflow",
                    "ref": "demo/workflows/child",
                },
                "wrap": {
                    "type": "kolsch/nodes/compute",
                    "value": "wrap-done",
                },
            },
            "graph": {
                "edges": [
                    {"from": "prep", "to": "sub"},
                    {"from": "sub", "to": "wrap"},
                ]
            },
        }
    )
    child_yaml = """
    nodes:
      only:
        type: kolsch/nodes/compute
        value: "leaf-output"
    graph:
      edges: []
    """
    store = WorkflowRunStore(tmp_path)
    catalog = _ext_compose_catalog(
        tmp_path=tmp_path,
        refs={"demo/workflows/child": child_yaml},
    )
    registry = _ext_compose_registry(catalog=catalog, store=store)
    return (
        workflow,
        registry,
        {
            "catalog": catalog,
            "fork_at_kind": "workflow.node.completed",
            "fork_at_node_name": "sub",
            "input_overrides": {},
        },
    )


_FORK_FIXTURES: tuple[
    tuple[
        str,
        Callable[[Path], tuple[Workflow, RunnerRegistry, dict[str, Any]]],
    ],
    ...,
] = (
    ("fork-at-completed", _build_fork_at_completed),
    ("fork-at-started", _build_fork_at_started),
    ("fork-with-input-override", _build_fork_with_input_override),
    ("fork-subworkflow-after", _build_fork_subworkflow_after),
)


def _find_fork_event_id(
    events: list[dict[str, Any]],
    *,
    kind: str,
    node_name: str | None,
) -> str:
    for event in events:
        if event.get("kind") != kind:
            continue
        if node_name is None:
            event_id: str = event["event_id"]
            return event_id
        en = event.get("node_name") or event.get("payload", {}).get("node_name")
        if en == node_name:
            matched_event_id: str = event["event_id"]
            return matched_event_id
    msg = (
        f"no event matching kind={kind!r} node_name={node_name!r}; "
        f"available kinds: {sorted({e['kind'] for e in events})}"
    )
    raise AssertionError(msg)


@pytest.mark.parametrize(
    ("fixture_name", "builder"),
    _FORK_FIXTURES,
    ids=[name for name, *_ in _FORK_FIXTURES],
)
async def test_workflow_fork_matches_paired_goldens(
    fixture_name: str,
    builder: Callable[[Path], tuple[Workflow, RunnerRegistry, dict[str, Any]]],
    tmp_path: Path,
) -> None:
    """Phase 3.8 — paired (parent, fork) goldens for fork scenarios.

    The same :class:`Canonicaliser` instance is threaded through
    BOTH JSONL files so shared ids (``trace_id`` always; the
    parent's ``workflow_run_id`` when the fork carries it in the
    ``forked_from_run_id`` payload field) resolve to identical
    sentinels in both goldens. Reviewer note from the build doc:
    canonicalisation MUST share state across the pair.
    """
    from schemas.entities import ForkedFrom

    workflow, registry, extras = builder(tmp_path)
    catalog = extras.get("catalog") or AssetCatalog()
    fork_at_kind: str = extras["fork_at_kind"]
    fork_at_node_name: str | None = extras["fork_at_node_name"]
    overrides: dict[str, Any] = extras.get("input_overrides") or {}

    store = WorkflowRunStore(tmp_path)
    parent_run = store.create(workflow_ref="demo/workflows/x", inputs={})
    parent_ex = IterativeExecutor(
        workflow=workflow,
        run=parent_run,
        store=store,
        registry=registry,
        catalog=catalog,
    )
    parent_status = await parent_ex.run()
    assert parent_status.value == "succeeded"

    parent_events = list(store.read_events(parent_run.run_id))
    fork_event_id = _find_fork_event_id(
        parent_events,
        kind=fork_at_kind,
        node_name=fork_at_node_name,
    )

    # Mint the forked run + fresh executor pointed at the same
    # workflow + store. This is the same shape the CLI uses.
    forked_run = store.create(
        workflow_ref=parent_run.workflow_ref,
        inputs=dict(parent_run.inputs),
        trace_id=parent_run.trace_id,
        forked_from=ForkedFrom(
            run_id=parent_run.run_id,
            fork_event_id=fork_event_id,
        ),
    )
    fork_ex = IterativeExecutor(
        workflow=workflow,
        run=forked_run,
        store=store,
        registry=registry,
        catalog=catalog,
    )
    fork_status = await fork_ex.start_from_fork(
        fork_event_id=fork_event_id,
        input_overrides=overrides,
    )
    assert fork_status.value in ("succeeded", "waiting_for_input")

    parent_raw = store.directory(parent_run.run_id).events_path.read_text(
        encoding="utf-8"
    )
    fork_raw = store.directory(forked_run.run_id).events_path.read_text(
        encoding="utf-8"
    )

    canon = Canonicaliser()
    parent_actual = canon.sub(parent_raw)
    fork_actual = canon.sub(fork_raw)

    pairs = (
        (f"{fixture_name}.parent", parent_actual),
        (f"{fixture_name}.fork", fork_actual),
    )
    for name, actual in pairs:
        golden_path = _GOLDEN_DIR / f"{name}.jsonl"
        _assert_or_update_golden(golden_path, actual, name)

    # The fork golden's first event MUST be workflow.run.forked_from
    # with parent_event_id=None — same first-event invariant that
    # workflow.run.resumed has for Phase 3.4 (verified inline so a
    # missing leading event surfaces here, not buried in a diff).
    first_fork_line = fork_raw.splitlines()[0]
    first_event = json.loads(first_fork_line)
    assert first_event["kind"] == "workflow.run.forked_from"
    assert first_event["parent_event_id"] is None


# Sanity check on the canonicaliser itself.
def test_canonicaliser_assigns_sentinels_in_first_occurrence_order() -> None:
    text = json.dumps(
        {
            "event_id": "ev_aaaaaaaa-1111-1111-1111-111111111111",
            "workflow_run_id": "wrk_bbbbbbbb-2222-2222-2222-222222222222",
            "ts": "2026-06-26T15:33:10.119266Z",
        }
    )
    canonical = canonicalize_jsonl(text)
    assert '"event_id": "ev_001"' in canonical
    assert '"workflow_run_id": "wrk_001"' in canonical
    assert "<TS>" in canonical


def test_canonicaliser_reuses_sentinel_for_same_id() -> None:
    text = '"ev_aaaaaaaa-1111-1111-1111-111111111111" "ev_aaaaaaaa-1111-1111-1111-111111111111"'
    canonical = canonicalize_jsonl(text)
    assert canonical.count("ev_001") == 2
    assert "ev_002" not in canonical


# ---------------------------------------------------------------------------
# Phase 3.2 §11.6 — workflow→agent cross-JSONL goldens
# ---------------------------------------------------------------------------


async def test_workflow_agent_roundtrip_matches_paired_goldens(
    tmp_path: Path,
) -> None:
    """Phase 3.2 Step 4 AC 4.3-4.4 — paired goldens cover the
    workflow JSONL AND the spawned agent session JSONL.

    A single :class:`Canonicaliser` instance is threaded through both
    files so any id that appears in BOTH (notably ``trace_id``) maps
    to the same sentinel — a sloppy split canonicaliser would mask
    the very correlation this golden is supposed to catch.

    To accept an intentional change to either golden, copy the
    ``actual`` block from the assertion's pytest output over the
    matching ``.jsonl`` under :data:`_GOLDEN_DIR` and re-run.
    """
    # Defer heavyweight imports to test time so this module is
    # importable when the engine wiring isn't available (e.g. lint
    # passes that don't install crewai).
    from tests.component.test_workflow_agent_runner_e2e import (
        _drive_workflow,
        _ScriptedLLM,
    )

    status, events_path, session_jsonl, _store = await _drive_workflow(
        tmp_path, llm=_ScriptedLLM(texts=["Hello, friend!"])
    )
    assert status == "succeeded"

    canon = Canonicaliser()
    workflow_actual = canon.sub(events_path.read_text(encoding="utf-8"))
    session_actual = canon.sub(session_jsonl.read_text(encoding="utf-8"))

    pairs = (
        ("workflow-agent-roundtrip.workflow", workflow_actual),
        ("workflow-agent-roundtrip.session", session_actual),
    )
    for name, actual in pairs:
        golden_path = _GOLDEN_DIR / f"{name}.jsonl"
        _assert_or_update_golden(golden_path, actual, name)

    # AC 4.4 — trace_id identity across both files. The shared
    # canonicaliser guarantees the same raw trace_id resolves to
    # the same sentinel; explicitly verifying tr_001 appears in
    # both is the documentary insurance against a future refactor
    # that accidentally splits the canonicaliser per file.
    assert (
        '"trace_id":"tr_001"' in workflow_actual
        or '"trace_id": "tr_001"' in workflow_actual
    )
    assert (
        '"trace_id":"tr_001"' in session_actual
        or '"trace_id": "tr_001"' in session_actual
    )
