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
        from workflows.errors import BashNonZeroExit  # noqa: PLC0415

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

    Uses a fake runner that mimics the catalog-runner output shape —
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
    # short-circuits the importlib introspection so the fake runner
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
            from workflows.errors import BashNonZeroExit  # noqa: PLC0415

            _ = (node_def, bound_inputs, state, ctx)
            captured = dict(self._by_node[node_name])
            if captured.get("exit_code", 0) != 0:
                raise BashNonZeroExit(
                    node=node_name, captured=captured
                )
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
    import textwrap  # noqa: PLC0415

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
        _StaticRunner(
            {"value": True, "category": "urgent", "score": 0.95}
        ),
    )
    registry.register("kolsch/nodes/noop", _StaticRunner({}))
    from assets.catalog import AssetEntry  # noqa: PLC0415

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
        _BashStubRunner(
            {"stdout": "", "stderr": "", "exit_code": 0}
        ),
    )
    registry.register("kolsch/nodes/noop", _StaticRunner({}))
    registry.register("kolsch/nodes/agent", _StaticRunner({}))
    _ = tmp_path
    return workflow, registry, {}


_FIXTURES: tuple[
    tuple[
        str,
        Callable[
            [Path], tuple[Workflow, RunnerRegistry, dict[str, Any]]
        ],
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
    builder: Callable[
        [Path], tuple[Workflow, RunnerRegistry, dict[str, Any]]
    ],
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

    actual_raw = store.directory(run.run_id).events_path.read_text(
        encoding="utf-8"
    )
    actual = canonicalize_jsonl(actual_raw)

    golden_path = _GOLDEN_DIR / f"{fixture_name}.jsonl"
    if not golden_path.exists():
        msg = (
            f"Missing golden for fixture {fixture_name!r}.\n"
            f"To create it, write the following text to {golden_path}:\n\n"
            f"{actual}"
        )
        raise AssertionError(msg)
    expected = golden_path.read_text(encoding="utf-8")
    assert actual == expected, (
        f"Golden mismatch for {fixture_name!r}.\n"
        f"--- expected ({golden_path}) ---\n{expected}"
        f"--- actual ---\n{actual}"
    )


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
    missing: list[str] = []
    for name, actual in pairs:
        golden_path = _GOLDEN_DIR / f"{name}.jsonl"
        if not golden_path.exists():
            missing.append(
                f"# write to {golden_path}\n{actual}"
            )
    if missing:
        msg = "Missing paired goldens.\n" + "\n---\n".join(missing)
        raise AssertionError(msg)

    for name, actual in pairs:
        golden_path = _GOLDEN_DIR / f"{name}.jsonl"
        expected = golden_path.read_text(encoding="utf-8")
        assert actual == expected, (
            f"Golden mismatch for {name!r}.\n"
            f"--- expected ({golden_path}) ---\n{expected}"
            f"--- actual ---\n{actual}"
        )

    # AC 4.4 — trace_id identity across both files. The shared
    # canonicaliser guarantees the same raw trace_id resolves to
    # the same sentinel; explicitly verifying tr_001 appears in
    # both is the documentary insurance against a future refactor
    # that accidentally splits the canonicaliser per file.
    assert '"trace_id":"tr_001"' in workflow_actual or '"trace_id": "tr_001"' in workflow_actual
    assert '"trace_id":"tr_001"' in session_actual or '"trace_id": "tr_001"' in session_actual
