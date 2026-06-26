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

from tests.golden._helpers import canonicalize_text

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
