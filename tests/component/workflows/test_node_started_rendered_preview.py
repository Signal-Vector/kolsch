"""bug-094 T1-T6 - NODE_STARTED carries rendered config previews + used inputs.

The executor emits two additive-optional fields on every
``workflow.node.started`` domain payload:

- ``node_config_preview`` — now carries a RENDERED companion
  (``rendered_command`` / ``rendered_value`` / ``rendered_over`` /
  ``rendered_binds``) alongside the raw template, for bash / compute / map
  / nested-workflow nodes. Agent (and other config-less) nodes emit ``{}``.
- ``workflow_inputs_used`` — the subset of the workflow's top-level
  ``inputs`` that the node's templated fields (config + inbound-edge binds)
  reference, via a static Jinja-AST walk (independent of render success).

These tests exercise all five emission sites (four in ``executor.py`` via
the sequential/concurrent + success/failure paths; one in
``builtins/map.py`` via the per-iteration path).
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING, Any

from assets.catalog import AssetCatalog, AssetEntry
from workflows.builtins import (
    ComputeRunner,
    MapRunner,
    NoopRunner,
    WorkflowSubRunner,
)
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path


class _StaticRunner:
    """Runner stub — the config preview is emitted at NODE_STARTED, BEFORE the
    runner runs, so a stub suffices for node types whose real runner would
    otherwise touch the shell / catalog (bash / agent)."""

    def __init__(self, output: dict[str, Any] | None = None) -> None:
        self._output = output or {}

    async def run(self, **_: Any) -> dict[str, Any]:
        return dict(self._output)


def _wf(payload: dict[str, Any]) -> Workflow:
    return Workflow.model_validate(payload)


def _make_executor(
    tmp_path: Path,
    *,
    workflow: Workflow,
    registry: RunnerRegistry,
    inputs: dict[str, Any] | None = None,
    catalog: AssetCatalog | None = None,
) -> tuple[IterativeExecutor, WorkflowRunStore, Any]:
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs=inputs or {})
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=catalog or AssetCatalog(),
    )
    return ex, store, run


def _started_events(store: WorkflowRunStore, run_id: Any) -> list[dict[str, Any]]:
    return [
        e for e in store.read_events(run_id) if e["kind"] == "workflow.node.started"
    ]


def _started_for(
    store: WorkflowRunStore, run_id: Any, node_name: str
) -> dict[str, Any]:
    for e in _started_events(store, run_id):
        if e["payload"]["node_name"] == node_name:
            payload: dict[str, Any] = e["payload"]
            return payload
    msg = f"no NODE_STARTED for {node_name!r}"
    raise AssertionError(msg)


# ---------------------------------------------------------------------------
# Test 1 — rendered previews for bash + compute + map + workflow (NOT agent).
# ---------------------------------------------------------------------------


def _write_workflow(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def _entry(ref: str, path: Path) -> AssetEntry:
    return AssetEntry(
        ref=ref,
        asset_type="workflow",
        bundle=ref.split("/", 1)[0],
        path=str(path),
    )


def _full_registry(*, catalog: AssetCatalog, store: WorkflowRunStore) -> RunnerRegistry:
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/noop", NoopRunner())
    registry.register("kolsch/nodes/compute", ComputeRunner())
    registry.register("kolsch/nodes/map", MapRunner(registry=registry))
    registry.register(
        "kolsch/nodes/bash", _StaticRunner({"stdout": "", "exit_code": 0})
    )
    registry.register("kolsch/nodes/agent", _StaticRunner({}))

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


async def test_rendered_previews_for_covered_types_plus_agent_empty(
    tmp_path: Path,
) -> None:
    """T1: bash/compute/map/workflow render; agent emits `{}` preview but a
    non-empty `workflow_inputs_used` from its inbound edge binds."""
    # Child workflow for the nested-workflow node.
    child_path = tmp_path / "bundles" / "demo" / "workflows" / "child.yaml"
    _write_workflow(
        child_path,
        """
        nodes:
          echo: {type: kolsch/nodes/compute, value: "{{ inputs.foo }}"}
        graph:
          edges: []
        """,
    )
    catalog = AssetCatalog(
        assets={"demo/workflows/child": _entry("demo/workflows/child", child_path)}
    )
    wf = _wf(
        {
            "nodes": {
                "b": {"type": "kolsch/nodes/bash", "command": "echo {{ inputs.foo }}"},
                "c": {"type": "kolsch/nodes/compute", "value": "{{ inputs.foo }}"},
                "m": {
                    "type": "kolsch/nodes/map",
                    "apply": "worker",
                    "over": "{{ [inputs.foo] }}",
                    "item_as": "item",
                },
                "worker": {
                    "type": "kolsch/nodes/compute",
                    "value": "{{ inputs.item }}",
                },
                "sub": {"type": "kolsch/nodes/workflow", "ref": "demo/workflows/child"},
                "ag": {"type": "kolsch/nodes/agent", "agent": "demo/agents/x"},
            },
            # Chain b → c → m → sub → ag so every node is reachable. `worker`
            # is the map.apply target (not a standalone root). The sub → ag
            # edge binds `foo` so the agent node surfaces workflow_inputs_used.
            "graph": {
                "edges": [
                    {"from": "b", "to": "c"},
                    {"from": "c", "to": "m"},
                    {"from": "m", "to": "sub", "binds": {"foo": "{{ inputs.foo }}"}},
                    {"from": "sub", "to": "ag", "binds": {"foo": "{{ inputs.foo }}"}},
                ]
            },
        }
    )
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={"foo": "bar"})
    registry = _full_registry(catalog=catalog, store=store)
    ex = IterativeExecutor(
        workflow=wf, run=run, store=store, registry=registry, catalog=catalog
    )
    status = await ex.run()
    assert status == WorkflowRunStatus.SUCCEEDED

    bash = _started_for(store, run.run_id, "b")
    assert bash["node_config_preview"]["command"] == "echo {{ inputs.foo }}"
    assert bash["node_config_preview"]["rendered_command"] == "echo bar"

    compute = _started_for(store, run.run_id, "c")
    assert compute["node_config_preview"]["value"] == "{{ inputs.foo }}"
    assert compute["node_config_preview"]["rendered_value"] == '"bar"'

    mp = _started_for(store, run.run_id, "m")
    assert mp["node_config_preview"]["over"] == "{{ [inputs.foo] }}"
    assert mp["node_config_preview"]["rendered_over"] == '["bar"]'

    sub = _started_for(store, run.run_id, "sub")
    assert sub["node_config_preview"]["ref"] == "demo/workflows/child"
    assert sub["node_config_preview"]["binds"] == {"foo": "{{ inputs.foo }}"}
    assert sub["node_config_preview"]["rendered_binds"] == {"foo": '"bar"'}

    # Agent nodes are NOT covered — empty config preview — but still surface
    # workflow_inputs_used from their inbound edge bind.
    ag = _started_for(store, run.run_id, "ag")
    assert ag["node_config_preview"] == {}
    assert ag["workflow_inputs_used"] == {"foo": "bar"}


# ---------------------------------------------------------------------------
# Test 2 — workflow_inputs_used per node (referenced only, no over-inclusion).
# ---------------------------------------------------------------------------


async def test_workflow_inputs_used_referenced_only(tmp_path: Path) -> None:
    """T2: A references `inputs.url`; B references nothing."""
    wf = _wf(
        {
            "nodes": {
                "a": {"type": "kolsch/nodes/bash", "command": "get {{ inputs.url }}"},
                "b": {"type": "kolsch/nodes/bash", "command": "echo hi"},
            },
            "graph": {"edges": [{"from": "a", "to": "b"}]},
        }
    )
    registry = RunnerRegistry()
    registry.register(
        "kolsch/nodes/bash", _StaticRunner({"stdout": "", "exit_code": 0})
    )
    ex, store, run = _make_executor(
        tmp_path,
        workflow=wf,
        registry=registry,
        inputs={"url": "https://x", "unused": "y"},
    )
    status = await ex.run()
    assert status == WorkflowRunStatus.SUCCEEDED

    a = _started_for(store, run.run_id, "a")
    assert a["workflow_inputs_used"] == {"url": "https://x"}
    b = _started_for(store, run.run_id, "b")
    assert b["workflow_inputs_used"] == {}


# ---------------------------------------------------------------------------
# Test 3 — render-failure fallback still emits both fields.
# ---------------------------------------------------------------------------


async def test_render_failure_fallback_emits_both_fields(tmp_path: Path) -> None:
    """T3: a fired-edge bind renders to Undefined; the synthetic started event
    still carries both new fields (raw command + rendered_command=None, empty
    workflow_inputs_used)."""
    wf = _wf(
        {
            "nodes": {
                "a": {"type": "kolsch/nodes/bash", "command": "echo hi"},
                "b": {"type": "kolsch/nodes/bash", "command": "get {{ inputs.url }}"},
            },
            # The edge binds `x` from a workflow input that is NOT supplied
            # (inputs={}), so `_render_bound_inputs` renders it to a Jinja
            # Undefined and raises WorkflowBindUndefined → render-failure
            # fallback path (site 1). Static validation permits `inputs.*`
            # refs (no declared inputs schema to check against).
            "graph": {
                "edges": [{"from": "a", "to": "b", "binds": {"x": "{{ inputs.url }}"}}]
            },
        }
    )
    registry = RunnerRegistry()
    registry.register(
        "kolsch/nodes/bash", _StaticRunner({"stdout": "", "exit_code": 0})
    )
    ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry, inputs={})
    status = await ex.run()
    assert status == WorkflowRunStatus.FAILED

    b = _started_for(store, run.run_id, "b")
    assert b["workflow_inputs_used"] == {}
    assert b["node_config_preview"]["command"] == "get {{ inputs.url }}"
    assert b["node_config_preview"]["rendered_command"] is None


# ---------------------------------------------------------------------------
# Test 4 — AST walk survives nodes.* and mixed references.
# ---------------------------------------------------------------------------


async def test_ast_walk_survives_nodes_and_mixed_refs(tmp_path: Path) -> None:
    """T4: compute value references both inputs.url and nodes.upstream.stdout;
    only inputs.url is picked up, nodes.* is ignored (no exception)."""
    wf = _wf(
        {
            "nodes": {
                "upstream": {
                    "type": "kolsch/nodes/compute",
                    "value": "hello",
                },
                "c": {
                    "type": "kolsch/nodes/compute",
                    "value": "{{ inputs.url }}-{{ nodes.upstream.value }}",
                },
            },
            "graph": {"edges": [{"from": "upstream", "to": "c"}]},
        }
    )
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/compute", ComputeRunner())
    ex, store, run = _make_executor(
        tmp_path, workflow=wf, registry=registry, inputs={"url": "https://x"}
    )
    status = await ex.run()
    assert status == WorkflowRunStatus.SUCCEEDED

    c = _started_for(store, run.run_id, "c")
    assert c["workflow_inputs_used"] == {"url": "https://x"}
    assert c["node_config_preview"]["rendered_value"] == '"https://x-hello"'


# ---------------------------------------------------------------------------
# Test 5 — map-iteration site emits both new fields.
# ---------------------------------------------------------------------------


async def test_map_iteration_site_emits_both_fields(tmp_path: Path) -> None:
    """T5: every per-iteration NODE_STARTED carries the worker's rendered
    command (with the workflow input substituted) + workflow_inputs_used."""
    wf = _wf(
        {
            "nodes": {
                "fan": {
                    "type": "kolsch/nodes/map",
                    "apply": "worker",
                    "over": [1, 2, 3],
                    "item_as": "item",
                },
                "worker": {
                    "type": "kolsch/nodes/bash",
                    "command": "run {{ inputs.batch_id }} {{ inputs.item }}",
                },
            },
            "graph": {"edges": []},
        }
    )
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/map", MapRunner(registry=registry))
    registry.register(
        "kolsch/nodes/bash", _StaticRunner({"stdout": "", "exit_code": 0})
    )
    ex, store, run = _make_executor(
        tmp_path, workflow=wf, registry=registry, inputs={"batch_id": "b42"}
    )
    status = await ex.run()
    assert status == WorkflowRunStatus.SUCCEEDED

    iters = [
        e
        for e in _started_events(store, run.run_id)
        if e["payload"]["node_name"] == "worker"
    ]
    assert len(iters) == 3
    for i, ev in enumerate(iters, start=1):
        payload = ev["payload"]
        # `item` is an iteration-overlay key (not a workflow input), so it is
        # excluded from workflow_inputs_used — only `batch_id` remains.
        assert payload["workflow_inputs_used"] == {"batch_id": "b42"}
        preview = payload["node_config_preview"]
        assert preview["command"] == "run {{ inputs.batch_id }} {{ inputs.item }}"
        assert preview["rendered_command"] == f"run b42 {i}"


# ---------------------------------------------------------------------------
# Test 6 — nested-workflow node exposes ref + rendered binds.
# ---------------------------------------------------------------------------


async def test_nested_workflow_exposes_ref_and_rendered_binds(
    tmp_path: Path,
) -> None:
    """T6: a kolsch/nodes/workflow node whose bind references inputs.url
    exposes ref + binds + rendered_binds + workflow_inputs_used."""
    child_path = tmp_path / "bundles" / "demo" / "workflows" / "child.yaml"
    _write_workflow(
        child_path,
        """
        nodes:
          echo: {type: kolsch/nodes/compute, value: "{{ inputs.url }}"}
        graph:
          edges: []
        """,
    )
    catalog = AssetCatalog(
        assets={"demo/workflows/child": _entry("demo/workflows/child", child_path)}
    )
    wf = _wf(
        {
            "nodes": {
                "seed": {"type": "kolsch/nodes/compute", "value": "x"},
                "sub": {"type": "kolsch/nodes/workflow", "ref": "demo/workflows/child"},
            },
            "graph": {
                "edges": [
                    {
                        "from": "seed",
                        "to": "sub",
                        "binds": {"url": "{{ inputs.url }}"},
                    }
                ]
            },
        }
    )
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={"url": "https://x"})
    registry = _full_registry(catalog=catalog, store=store)
    ex = IterativeExecutor(
        workflow=wf, run=run, store=store, registry=registry, catalog=catalog
    )
    status = await ex.run()
    assert status == WorkflowRunStatus.SUCCEEDED

    sub = _started_for(store, run.run_id, "sub")
    preview = sub["node_config_preview"]
    assert preview["ref"] == "demo/workflows/child"
    assert preview["binds"] == {"url": "{{ inputs.url }}"}
    assert preview["rendered_binds"] == {"url": '"https://x"'}
    assert sub["workflow_inputs_used"] == {"url": "https://x"}
