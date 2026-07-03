"""Phase 3.6 Step 1 — NativeEnvironment typed-vs-string regression.

``src/workflows/jinja_env.py`` is a hybrid
:class:`WorkflowJinjaEnvironment(ImmutableSandboxedEnvironment)`
using ``NativeCodeGenerator`` + ``native_concat`` so:

- **Single-expression templates** (``"{{ expr }}"``) return the raw
  Python value Jinja produced. ``"{{ 1 + 1 }}"`` is the int ``2``,
  ``"{{ items }}"`` is the actual ``list``, ``"{{ a == b }}"`` is
  the bool ``True``.
- **Multi-segment templates** (``"echo {{ name }}"``) concatenate
  to strings — preserves bash command semantics where the shell
  needs a string argv.

This module locks both halves so a future Jinja-env regression can't
silently flip a typed return into a stringified one (which would
manifest downstream as obscure shell-quoting failures or comparison
mismatches). Sits alongside
:mod:`tests.component.test_workflow_compute_recipes` — the recipe
suite asserts per-recipe types; this suite asserts the substrate
that enables them.

Naming-collision note: see :mod:`tests.component.test_workflow_compute_recipes`
docstring — ``inputs.items`` resolves to ``dict.items`` under
Jinja, so list-typed fixtures bind as ``records`` / ``numbers``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from assets.catalog import AssetCatalog
from workflows.builtins import ComputeRunner, FilterRunner
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wf(payload: dict[str, Any]) -> Workflow:
    return Workflow.model_validate(payload)


def _registry() -> RunnerRegistry:
    reg = RunnerRegistry()
    reg.register("kolsch/nodes/compute", ComputeRunner())
    reg.register("kolsch/nodes/filter", FilterRunner())
    return reg


def _make_executor(
    tmp_path: Path,
    *,
    workflow: Workflow,
    inputs: dict[str, Any] | None = None,
    registry: RunnerRegistry | None = None,
) -> tuple[IterativeExecutor, WorkflowRunStore, Any]:
    store = WorkflowRunStore(tmp_path)
    run = store.create(
        workflow_ref="demo/workflows/native-env",
        inputs=inputs or {},
    )
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry or _registry(),
        catalog=AssetCatalog(),
    )
    return ex, store, run


async def _compute(
    tmp_path: Path, *, expression: str, inputs: dict[str, Any] | None = None
) -> Any:
    wf = _wf(
        {
            "nodes": {
                "calc": {
                    "type": "kolsch/nodes/compute",
                    "value": expression,
                },
            }
        }
    )
    ex, _store, _run = _make_executor(tmp_path, workflow=wf, inputs=inputs)
    await ex.run()
    return ex._captured["calc"]["value"]


# ---------------------------------------------------------------------------
# Single-expression templates — typed Python value preserved
# ---------------------------------------------------------------------------


class TestSingleExpressionPreservesType:
    async def test_int_arithmetic_returns_int(
        self, tmp_path: Path
    ) -> None:
        value = await _compute(tmp_path, expression="{{ 1 + 1 }}")
        assert value == 2
        assert isinstance(value, int)
        assert not isinstance(value, bool)

    async def test_list_length_returns_int(
        self, tmp_path: Path
    ) -> None:
        value = await _compute(
            tmp_path,
            expression="{{ inputs.numbers | length }}",
            inputs={"numbers": [10, 20, 30]},
        )
        assert value == 3
        assert isinstance(value, int)

    async def test_list_passes_through_typed(
        self, tmp_path: Path
    ) -> None:
        value = await _compute(
            tmp_path,
            expression="{{ inputs.numbers }}",
            inputs={"numbers": [1, 2, 3]},
        )
        assert value == [1, 2, 3]
        assert isinstance(value, list)

    async def test_bool_comparison_returns_bool(
        self, tmp_path: Path
    ) -> None:
        value = await _compute(tmp_path, expression="{{ 1 == 1 }}")
        assert value is True
        assert isinstance(value, bool)

    async def test_dict_attribute_access_returns_value(
        self, tmp_path: Path
    ) -> None:
        value = await _compute(
            tmp_path,
            expression="{{ inputs.ticket.urgent }}",
            inputs={"ticket": {"urgent": True, "id": 42}},
        )
        assert value is True
        assert isinstance(value, bool)


# ---------------------------------------------------------------------------
# Multi-segment templates — concatenate to strings
# ---------------------------------------------------------------------------


class TestMultiSegmentReturnsString:
    async def test_string_concatenation_returns_string(
        self, tmp_path: Path
    ) -> None:
        value = await _compute(
            tmp_path, expression="{{ 'hello ' + 'world' }}"
        )
        # Single-expression with string operands — NativeEnvironment
        # returns the typed result (``str``). The "multi-segment"
        # case is structurally distinct; covered by the bash test
        # below.
        assert value == "hello world"
        assert isinstance(value, str)

    async def test_multi_segment_template_returns_string(
        self, tmp_path: Path
    ) -> None:
        # ``"prefix-{{ inputs.x }}-suffix"`` is structurally
        # multi-segment — NativeEnvironment falls back to string
        # concatenation. The compute runner captures the str.
        value = await _compute(
            tmp_path,
            expression="prefix-{{ inputs.x }}-suffix",
            inputs={"x": 42},
        )
        assert value == "prefix-42-suffix"
        assert isinstance(value, str)


# ---------------------------------------------------------------------------
# Bash command-string non-regression — must NOT flip to typed
# ---------------------------------------------------------------------------


class TestBashCommandStringNonRegression:
    async def test_bash_command_renders_string(
        self, tmp_path: Path
    ) -> None:
        """Bash's ``command:`` must render to a ``str`` the shell can
        execute. A typed return (list, int, etc.) would crash the
        shell or be silently misinterpreted. This is the load-bearing
        non-regression — if NativeEnvironment ever stringifies
        differently, this fires first.
        """
        from tests.golden.runs_test import _StaticRunner

        # Use the existing static bash stub to avoid standing up the
        # full tool-dispatch pipeline; the assertion is on the
        # captured stdout the workflow surfaces after rendering.
        wf = _wf(
            {
                "nodes": {
                    "say": {
                        "type": "kolsch/nodes/bash",
                        "command": "echo {{ inputs.name }}",
                    }
                }
            }
        )
        registry = RunnerRegistry()
        # The static runner doesn't exercise the actual render path,
        # so we cross-check via the executor's render_jinja directly.
        registry.register(
            "kolsch/nodes/bash",
            _StaticRunner({"stdout": "ok\n", "stderr": "", "exit_code": 0}),
        )
        ex, _store, _run = _make_executor(
            tmp_path,
            workflow=wf,
            inputs={"name": "world"},
            registry=registry,
        )
        await ex.run()
        # Render via the executor's surface — this is the path
        # BashRunner uses internally.
        rendered = ex._render_jinja(
            "echo {{ inputs.name }}",
            inputs_overlay={"name": "world"},
        )
        assert rendered == "echo world"
        assert isinstance(rendered, str)


# ---------------------------------------------------------------------------
# Filter → compute typed chain — round-trip preserves types
# ---------------------------------------------------------------------------


class TestFilterComputeTypedChain:
    async def test_filter_then_compute_typed_chain(
        self, tmp_path: Path
    ) -> None:
        """``filter`` produces ``{result: list}``; downstream
        ``compute`` consumes it via ``{{ nodes.f.result | length }}``
        and returns an ``int``. Round-trip proves typed values flow
        through edge captures without a string-coerce in the middle.
        """
        wf = _wf(
            {
                "nodes": {
                    "f": {
                        "type": "kolsch/nodes/filter",
                        "over": "{{ inputs.numbers }}",
                        "condition": "{{ inputs.item > 5 }}",
                    },
                    "c": {
                        "type": "kolsch/nodes/compute",
                        "value": "{{ nodes.f.result | length }}",
                    },
                },
                "graph": {
                    "edges": [{"from": "f", "to": "c"}],
                },
            }
        )
        ex, _store, _run = _make_executor(
            tmp_path, workflow=wf, inputs={"numbers": [3, 7, 5, 10]}
        )
        await ex.run()
        captured = ex._captured["c"]["value"]
        assert captured == 2
        assert isinstance(captured, int)
        assert not isinstance(captured, bool)
