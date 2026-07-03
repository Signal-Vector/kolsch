"""Phase 3.6 Step 1 — 8 aggregation recipes via :class:`ComputeRunner`.

Master spec §4.0 lines 234-245 — the 8 aggregation node types
(``sum`` / ``count`` / ``min`` / ``max`` / ``sort`` / ``flatten``
/ ``any`` / ``all``) DROPPED in favour of stock Jinja filters
expressed as ``compute.value:`` expressions. This module locks the
typed-return contract for each recipe so a future Jinja env or
NativeEnvironment regression that flips a typed return into a
stringified one trips the suite immediately.

Each parametrized case asserts BOTH the captured value AND its
Python type — a bare ``== expected`` would pass even if the value
were the string ``"2"`` (Python ``2 == "2"`` is False, but other
shapes like ``[]`` vs ``"[]"`` are subtler — explicit
``isinstance(...)`` is the contract).

Naming-collision note: the build doc's table refers to the fixture
collection as ``items``. We bind it as ``records`` instead because
``inputs.items`` resolves to ``dict.items`` (the method) under
Jinja's ``getattr``-first attribute resolution, masking the value
silently. Same convention :mod:`tests.component.test_workflow_filter`
uses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from assets.catalog import AssetCatalog
from workflows.builtins import ComputeRunner
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixture data + recipe table
# ---------------------------------------------------------------------------


_FIXTURE_RECORDS: list[dict[str, Any]] = [
    {"name": "alpha", "score": 10, "urgent": False},
    {"name": "beta", "score": 5, "urgent": True},
    {"name": "gamma", "score": 7, "urgent": False},
]


# (recipe_name, value_expression, expected_type, expected_value)
RECIPES: tuple[tuple[str, str, type, Any], ...] = (
    (
        "sum",
        "{{ inputs.records | sum(attribute='score') }}",
        int,
        22,
    ),
    (
        "count",
        "{{ inputs.records | length }}",
        int,
        3,
    ),
    (
        "min",
        "{{ inputs.records | min(attribute='score') }}",
        dict,
        {"name": "beta", "score": 5, "urgent": True},
    ),
    (
        "max",
        "{{ inputs.records | max(attribute='score') }}",
        dict,
        {"name": "alpha", "score": 10, "urgent": False},
    ),
    (
        "sort",
        "{{ inputs.records | sort(attribute='score', reverse=true) }}",
        list,
        [
            {"name": "alpha", "score": 10, "urgent": False},
            {"name": "gamma", "score": 7, "urgent": False},
            {"name": "beta", "score": 5, "urgent": True},
        ],
    ),
    (
        # ``sum(start=[])`` is the canonical Jinja idiom for flattening
        # one level — the master spec calls it out at §4.0. The literal
        # nested list keeps the test independent of fixture shape.
        "flatten",
        "{{ [[1, 2], [3, 4]] | sum(start=[]) }}",
        list,
        [1, 2, 3, 4],
    ),
    (
        "any",
        "{{ (inputs.records | selectattr('urgent') | list | length) > 0 }}",
        bool,
        True,
    ),
    (
        "all",
        "{{ (inputs.records | rejectattr('urgent') | list | length) == 0 }}",
        bool,
        False,
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _registry() -> RunnerRegistry:
    reg = RunnerRegistry()
    reg.register("kolsch/nodes/compute", ComputeRunner())
    return reg


async def _run_recipe(tmp_path: Path, *, expression: str) -> dict[str, Any]:
    workflow = Workflow.model_validate(
        {
            "nodes": {
                "agg": {
                    "type": "kolsch/nodes/compute",
                    "value": expression,
                },
            }
        }
    )
    store = WorkflowRunStore(tmp_path)
    run = store.create(
        workflow_ref="demo/workflows/recipes",
        inputs={"records": _FIXTURE_RECORDS},
    )
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=_registry(),
        catalog=AssetCatalog(),
    )
    await ex.run()
    return ex._captured["agg"]


# ---------------------------------------------------------------------------
# Parametrized harness — one test per recipe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("recipe_name", "expression", "expected_type", "expected_value"),
    RECIPES,
    ids=[r[0] for r in RECIPES],
)
async def test_aggregation_recipe(
    recipe_name: str,
    expression: str,
    expected_type: type,
    expected_value: Any,
    tmp_path: Path,
) -> None:
    """Each recipe captures the correctly-typed result."""
    captured = await _run_recipe(tmp_path, expression=expression)
    value = captured["value"]
    # Value equality first.
    assert value == expected_value, (
        f"recipe {recipe_name!r}: expected value {expected_value!r}, got {value!r}"
    )
    # Type contract — guards against a stringification regression
    # that would silently mask a broken NativeEnvironment.
    assert isinstance(value, expected_type), (
        f"recipe {recipe_name!r}: expected {expected_type.__name__}, "
        f"got {type(value).__name__} (value={value!r})"
    )
    # Defensive: ``bool`` is a subclass of ``int``; recipes that
    # expect ``int`` MUST NOT silently accept a ``bool``.
    if expected_type is int:
        assert not isinstance(value, bool)
