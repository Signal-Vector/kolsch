"""Coverage tests for the concurrent worklist + config-preview helpers.

Two independent surfaces of :mod:`workflows.executor`:

- The ``auto``-mode concurrent worklist's render-failure path (a
  fired-edge bind that renders to a Jinja ``Undefined`` inside
  ``_prepare_concurrent_dispatch`` raises, and the loop emits a
  synthetic ``started``/``failed``/``run.failed`` triple), plus the
  concurrent success finalizer's branch-value / bash-nonzero-branch
  emissions.
- The pure preview helpers (``_build_config_preview``,
  ``_preview_over_list``, ``_truncate_preview``, ``_extract_input_refs``,
  ``_node_type_ref``) exercised directly on constructed node defs.

The concurrent tests reuse the same ``StaticRunner`` /
``_make_executor`` harness shape as the sibling executor tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from assets.catalog import AssetCatalog
from workflows.builtins.branch import BranchRunner
from workflows.executor import (
    IterativeExecutor,
    RunnerRegistry,
    _build_config_preview,
    _dumps_preview,
    _extract_input_refs,
    _node_type_ref,
    _preview_over_list,
    _render_unavailable,
    _truncate_preview,
)
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path


class StaticRunner:
    """A :class:`NodeRunner` that returns a fixed output dict."""

    def __init__(self, output: dict[str, Any] | None = None) -> None:
        self._output = dict(output) if output is not None else {}

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
) -> tuple[IterativeExecutor, WorkflowRunStore, Any]:
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=AssetCatalog(),
    )
    return ex, store, run


# ---------------------------------------------------------------------------
# Concurrent worklist — render-failure (bind Undefined) path.
# ---------------------------------------------------------------------------


async def test_concurrent_bind_undefined_emits_synthetic_failed_triple(
    tmp_path: Path,
) -> None:
    """Under ``concurrency: auto`` a fired-edge bind that renders to a
    Jinja ``Undefined`` inside ``_prepare_concurrent_dispatch`` raises;
    the worklist emits a synthetic ``started``/``failed`` pair for the
    node plus ``run.failed`` and returns FAILED.

    This exercises the concurrent render-failure block that mirrors the
    sequential B2 guard — the terminal must be serializable and the
    failed payload must name the ``WorkflowBindUndefined`` class.
    """
    wf = _wf(
        {
            "concurrency": "auto",
            "nodes": {
                "src": {
                    "type": "kolsch/nodes/agent",
                    "agent": "demo/agents/mystery",
                },
                "sink": {
                    "type": "kolsch/nodes/agent",
                    "agent": "demo/agents/receiver",
                },
            },
            "graph": {
                "edges": [
                    {
                        "from": "src",
                        "to": "sink",
                        "binds": {
                            "indexed_files": "{{ nodes.src.indexed_files }}",
                        },
                    }
                ]
            },
        }
    )
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/agent", StaticRunner({"text": "hi"}))
    ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)

    status = await ex.run()
    assert status == WorkflowRunStatus.FAILED

    events = list(store.read_events(run.run_id))
    kinds = [e["kind"] for e in events]
    # Concurrent path still emits the started event for the sink node
    # before its synthetic failure, then run.failed as the terminal.
    assert "workflow.node.started" in kinds, kinds
    assert "workflow.node.failed" in kinds, kinds
    assert kinds[-1] == "workflow.run.failed", kinds

    failed = next(e for e in events if e["kind"] == "workflow.node.failed")
    payload = failed["payload"]
    assert payload["error_class"] == "WorkflowBindUndefined", payload
    assert payload["node_name"] == "sink", payload
    # The started event carried the render-unavailable config preview
    # (all rendered companions None) — agent nodes have no inline
    # config so this is `{}`, but the emission must not have crashed.
    started = next(e for e in events if e["kind"] == "workflow.node.started")
    assert started["payload"]["node_name"] in {"src", "sink"}


async def test_concurrent_branch_nonzero_bash_succeeds(tmp_path: Path) -> None:
    """A branchable bash node under ``concurrency: auto`` whose runner
    raises ``BashNonZeroExit`` is treated as success-of-the-false-arm.

    This drives the concurrent invoke path's ``BashNonZeroExit`` catch
    (returns ``bash_exc.captured``) and the ``_finalize_concurrent_success``
    ``branch_value`` payload emission for a branchable source.
    """
    from workflows.errors import BashNonZeroExit

    class NonZeroBashRunner:
        """Raise ``BashNonZeroExit`` to mimic a non-zero bash exit."""

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
            raise BashNonZeroExit(
                node=node_name,
                captured={"stdout": "", "stderr": "boom", "exit_code": 3},
            )

    wf = _wf(
        {
            "concurrency": "auto",
            "nodes": {
                "gate": {"type": "kolsch/nodes/bash", "command": "false"},
                "yes_arm": {"type": "kolsch/nodes/noop"},
                "no_arm": {"type": "kolsch/nodes/noop"},
            },
            "graph": {
                "edges": [
                    {"from": "gate", "to": "yes_arm", "branch": True},
                    {"from": "gate", "to": "no_arm", "branch": False},
                ]
            },
        }
    )
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/bash", NonZeroBashRunner())
    registry.register("kolsch/nodes/noop", StaticRunner())
    ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)

    status = await ex.run()
    assert status == WorkflowRunStatus.SUCCEEDED

    events = list(store.read_events(run.run_id))
    # The gate completed carrying a branch_value=False (non-zero exit).
    gate_completed = next(
        e
        for e in events
        if e["kind"] == "workflow.node.completed"
        and e["payload"]["node_name"] == "gate"
    )
    assert gate_completed["payload"].get("branch_value") is False
    # The true arm was pruned; only the false arm fired.
    completed_nodes = {
        e["payload"]["node_name"]
        for e in events
        if e["kind"] == "workflow.node.completed"
    }
    assert "no_arm" in completed_nodes
    assert "yes_arm" not in completed_nodes


async def test_concurrent_branch_true_prunes_false_arm(tmp_path: Path) -> None:
    """A ``kolsch/nodes/branch`` under ``concurrency: auto`` that returns
    ``True`` prunes the false arm — the concurrent success finalizer's
    branch-dispatch + cascade-prune path.
    """
    wf = _wf(
        {
            "concurrency": "auto",
            "nodes": {
                "decider": {
                    "type": "kolsch/nodes/branch",
                    "condition": "{{ true }}",
                },
                "t_arm": {"type": "kolsch/nodes/noop"},
                "f_arm": {"type": "kolsch/nodes/noop"},
            },
            "graph": {
                "edges": [
                    {"from": "decider", "to": "t_arm", "branch": True},
                    {"from": "decider", "to": "f_arm", "branch": False},
                ]
            },
        }
    )
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/branch", BranchRunner())
    registry.register("kolsch/nodes/noop", StaticRunner())
    ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)

    status = await ex.run()
    assert status == WorkflowRunStatus.SUCCEEDED

    events = list(store.read_events(run.run_id))
    completed_nodes = {
        e["payload"]["node_name"]
        for e in events
        if e["kind"] == "workflow.node.completed"
    }
    assert "t_arm" in completed_nodes
    assert "f_arm" not in completed_nodes
    # A pruned event fired for the false arm's subtree.
    kinds = [e["kind"] for e in events]
    assert "workflow.node.pruned" in kinds, kinds


async def test_concurrent_when_skip_cascades(tmp_path: Path) -> None:
    """A node whose ``when:`` gate is false under ``concurrency: auto``
    is silently skipped (no ``started`` event) and its downstream
    subgraph is cascade-pruned — the concurrent when-skip path."""
    wf = _wf(
        {
            "concurrency": "auto",
            "nodes": {
                "start": {"type": "kolsch/nodes/noop"},
                "gated": {
                    "type": "kolsch/nodes/noop",
                    "when": "{{ false }}",
                },
                "downstream": {"type": "kolsch/nodes/noop"},
            },
            "graph": {
                "edges": [
                    {"from": "start", "to": "gated"},
                    {"from": "gated", "to": "downstream"},
                ]
            },
        }
    )
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/noop", StaticRunner())
    ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)

    status = await ex.run()
    assert status == WorkflowRunStatus.SUCCEEDED

    events = list(store.read_events(run.run_id))
    started_nodes = {
        e["payload"]["node_name"]
        for e in events
        if e["kind"] == "workflow.node.started"
    }
    # `gated` (when-skipped) never emits a started event; nor does its
    # cascade-pruned `downstream` consumer.
    assert "gated" not in started_nodes
    assert "downstream" not in started_nodes
    assert "start" in started_nodes


async def test_concurrent_set_state_takes_mutation_lock(tmp_path: Path) -> None:
    """A ``kolsch/nodes/set_state`` node under ``concurrency: auto``
    runs under the state-mutation lock and writes its declared key into
    workflow state — the ``touches_state`` arm of the concurrent
    invoke path."""
    from workflows.builtins import SetStateRunner

    wf = _wf(
        {
            "concurrency": "auto",
            "nodes": {
                "writer": {
                    "type": "kolsch/nodes/set_state",
                    "keys": {"answer": "{{ 42 }}"},
                },
            },
            "graph": {"edges": []},
        }
    )
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/set_state", SetStateRunner())
    ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)

    status = await ex.run()
    assert status == WorkflowRunStatus.SUCCEEDED

    events = list(store.read_events(run.run_id))
    kinds = [e["kind"] for e in events]
    assert "workflow.state.mutated" in kinds, kinds


# ---------------------------------------------------------------------------
# _build_config_preview — one branch per renderable node type.
# ---------------------------------------------------------------------------


def _node(payload: dict[str, Any]) -> Any:
    """Build a single NodeDef by round-tripping through the parser."""
    wf = _wf({"nodes": {"n": payload}, "graph": {"edges": []}})
    return wf.nodes["n"]


def _identity_render(source: str) -> Any:
    """A render callable that returns its input unchanged."""
    return source


def test_config_preview_bash_renders_command() -> None:
    """A bash node's preview carries the raw + rendered ``command``."""
    node_def = _node({"type": "kolsch/nodes/bash", "command": "echo hi"})
    preview = _build_config_preview(
        node_def,
        workflow_inputs={},
        bound_inputs={},
        render=_identity_render,
    )
    assert preview == {"command": "echo hi", "rendered_command": "echo hi"}


def test_config_preview_compute_renders_value() -> None:
    """A compute node's string ``value`` is rendered + JSON-preview'd."""
    node_def = _node({"type": "kolsch/nodes/compute", "value": "{{ 1 + 1 }}"})
    preview = _build_config_preview(
        node_def,
        workflow_inputs={},
        bound_inputs={},
        render=lambda _s: 2,
    )
    assert preview["value"] == "{{ 1 + 1 }}"
    # Rendered value is JSON-encoded (an int becomes the string "2").
    assert preview["rendered_value"] == "2"


def test_config_preview_map_renders_over_list() -> None:
    """A map node's ``over:`` renders to a capped JSON list preview."""
    node_def = _node(
        {
            "type": "kolsch/nodes/map",
            "over": "{{ items }}",
            "apply": "worker",
            "item_as": "item",
        }
    )
    preview = _build_config_preview(
        node_def,
        workflow_inputs={},
        bound_inputs={},
        render=lambda _s: [1, 2, 3],
    )
    assert preview["over"] == "{{ items }}"
    assert preview["rendered_over"] == "[1, 2, 3]"


def test_config_preview_workflow_renders_binds() -> None:
    """A sub-workflow node's preview renders each Jinja bind and leaves
    a bare (non-Jinja) bind value as-is."""
    node_def = _node({"type": "kolsch/nodes/workflow", "ref": "demo/workflows/child"})
    raw_binds = {"jinja_key": "{{ nodes.x.out }}", "bare_key": "literal"}
    preview = _build_config_preview(
        node_def,
        workflow_inputs={},
        bound_inputs={},
        render=lambda _s: "rendered",
        raw_binds=raw_binds,
    )
    assert preview["ref"] == "demo/workflows/child"
    assert preview["binds"] == raw_binds
    # Jinja bind rendered; bare bind rendered to itself.
    assert preview["rendered_binds"]["jinja_key"] == '"rendered"'
    assert preview["rendered_binds"]["bare_key"] == '"literal"'


def test_config_preview_noop_is_empty() -> None:
    """A node type with no renderable inline config returns ``{}``."""
    node_def = _node({"type": "kolsch/nodes/noop"})
    preview = _build_config_preview(
        node_def,
        workflow_inputs={},
        bound_inputs={},
        render=_identity_render,
    )
    assert preview == {}


def test_config_preview_render_unavailable_yields_none_companions() -> None:
    """The ``_render_unavailable`` sentinel forces every rendered
    companion to ``None`` (its render call always raises, and
    ``_render_preview_or_none`` swallows it)."""
    node_def = _node({"type": "kolsch/nodes/bash", "command": "echo hi"})
    preview = _build_config_preview(
        node_def,
        workflow_inputs={},
        bound_inputs={},
        render=_render_unavailable,
    )
    assert preview == {"command": "echo hi", "rendered_command": None}


# ---------------------------------------------------------------------------
# _preview_over_list / _truncate_preview / _dumps_preview.
# ---------------------------------------------------------------------------


def test_preview_over_list_caps_with_more_sentinel() -> None:
    """A list longer than the cap is truncated with a ``…N more`` tail."""
    rendered = list(range(25))
    preview = _preview_over_list(rendered)
    assert "…5 more" in preview


def test_preview_over_list_non_list_falls_through() -> None:
    """A non-list rendered ``over:`` is JSON-encoded whole."""
    preview = _preview_over_list({"k": "v"})
    assert preview == '{"k": "v"}'


def test_truncate_preview_tail_truncates_over_cap() -> None:
    """A string longer than the cap is truncated with an ellipsis."""
    long_text = "x" * 3000
    out = _truncate_preview(long_text)
    assert out.endswith("…")
    assert len(out) <= 2000


def test_truncate_preview_short_passes_through() -> None:
    """A short string is returned unchanged."""
    assert _truncate_preview("short") == "short"


def test_dumps_preview_falls_back_to_repr() -> None:
    """A non-JSON-native value is encoded via ``default=repr``."""

    class Weird:
        def __repr__(self) -> str:
            return "WEIRD"

    out = _dumps_preview(Weird())
    assert "WEIRD" in out


# ---------------------------------------------------------------------------
# _extract_input_refs — static AST walk of inputs.* references.
# ---------------------------------------------------------------------------


def test_extract_input_refs_getattr_and_getitem() -> None:
    """Both ``inputs.attr`` and ``inputs['key']`` forms are collected;
    ``nodes.*`` / ``state.*`` are ignored."""
    src = "{{ inputs.alpha }}-{{ inputs['beta'] }}-{{ nodes.x.out }}-{{ state.s }}"
    refs = _extract_input_refs(src)
    assert refs == {"alpha", "beta"}


def test_extract_input_refs_malformed_template_yields_empty() -> None:
    """An unparseable template contributes no refs (the parse guard)."""
    assert _extract_input_refs("{{ this is ( not valid") == set()


def test_extract_input_refs_no_refs_yields_empty() -> None:
    """A bare literal string has no input refs."""
    assert _extract_input_refs("just a literal") == set()


# ---------------------------------------------------------------------------
# _node_type_ref — copy_of synthetic ref.
# ---------------------------------------------------------------------------


def test_node_type_ref_copy_of_is_synthetic() -> None:
    """A ``copy_of:`` node carries no ``type:`` — the ref falls back to
    the synthetic ``<copy_of>`` marker."""
    wf = _wf(
        {
            "nodes": {
                "orig": {"type": "kolsch/nodes/noop"},
                "clone": {"copy_of": "orig"},
            },
            "graph": {"edges": [{"from": "orig", "to": "clone"}]},
        }
    )
    assert _node_type_ref(wf.nodes["clone"]) == "<copy_of>"
    assert _node_type_ref(wf.nodes["orig"]) == "kolsch/nodes/noop"
