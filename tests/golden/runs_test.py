"""Golden workflow-run trace tests — Phase 3.1 Step 6.

Each fixture under :func:`_FIXTURES` runs a small workflow scenario
to completion, captures the JSONL produced by the executor, and
asserts semantic equality with a stored golden under
``tests/golden/runs/``. Volatile fields are canonicalised before
comparison so a fresh test run never re-emits a churning diff.

Canonical volatile-field substitution table (Round 7 A7-3):

    ts          → "<ts>"            timestamp string
    ev_<uuid>   → "<ev_0>"…         per-occurrence sentinel
    wrk_<uuid>  → "<wrk_0>"…        per-occurrence sentinel
    tr_<uuid>   → "<tr_0>"…         per-occurrence sentinel
    inv_<uuid>  → "<inv_0>"…        per-occurrence sentinel
    tc_<uuid>   → "<tc_0>"…         per-occurrence sentinel

The sentinels increment in **order of first occurrence in the JSONL
text** so two events sharing the same id resolve to the same sentinel
even across distant lines. The substitution table is intentionally
principled — no per-fixture ad-hoc replacements.

Updating goldens: run a fixture, copy the printed-on-failure
``actual`` block over the golden file, re-run.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from assets.catalog import AssetCatalog
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run_store import WorkflowRunStore

# ---------------------------------------------------------------------------
# Canonicaliser
# ---------------------------------------------------------------------------

_PREFIX_PATTERN = re.compile(
    r"\b(ev|wrk|tr|inv|tc)_[0-9a-fA-F-]{8,}\b"
)
_TS_PATTERN = re.compile(
    r'"ts":\s*"[^"]+"'
)


def canonicalize_jsonl(text: str) -> str:
    """Replace volatile id + timestamp fields with stable sentinels.

    See module docstring for the substitution table. The result is a
    stable string suitable for ``==`` comparison against a golden
    file.
    """
    counters: dict[str, int] = {}
    seen: dict[str, str] = {}

    def _sub_id(match: re.Match[str]) -> str:
        token = match.group(0)
        if token in seen:
            return seen[token]
        prefix = match.group(1)
        idx = counters.get(prefix, 0)
        sentinel = f"<{prefix}_{idx}>"
        counters[prefix] = idx + 1
        seen[token] = sentinel
        return sentinel

    text = _PREFIX_PATTERN.sub(_sub_id, text)
    return _TS_PATTERN.sub('"ts": "<ts>"', text)


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
            "ts": "2026-06-26T00:00:00Z",
        }
    )
    canonical = canonicalize_jsonl(text)
    assert "<ev_0>" in canonical
    assert "<wrk_0>" in canonical
    assert "<ts>" in canonical


def test_canonicaliser_reuses_sentinel_for_same_id() -> None:
    text = '"ev_aaaaaaaa-1111-1111-1111-111111111111" "ev_aaaaaaaa-1111-1111-1111-111111111111"'
    canonical = canonicalize_jsonl(text)
    assert canonical.count("<ev_0>") == 2
    assert "<ev_1>" not in canonical
