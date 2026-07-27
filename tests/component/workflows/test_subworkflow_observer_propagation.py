"""spec-103 T1/T2 — the child executor receives the parent's observers.

When a `kolsch/nodes/workflow` node spawns a child run, the parent
executor's `event_observer` / `sub_agent_event_observer` are threaded
into the nested (child) executor via the new `NodeContext` fields
(`event_observer`, `sub_agent_event_observer`) populated in
`IterativeExecutor._build_ctx` and forwarded by `WorkflowSubRunner`.

T1 asserts a recording `event_observer` captures events carrying BOTH
the parent's AND the child's `workflow_run_id` — i.e. the child's
RUN_STARTED / NODE_* / RUN_COMPLETED reached the same observer channel.
Before the fix the recorder would see only the parent's run_id (plus
the two `called_workflow` markers, which are parent-side events).

T2 asserts the `NodeContext` built by `_build_ctx` carries the observer
handles (or `None` when the executor was built without them).
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING, Any

from assets.catalog import AssetCatalog, AssetEntry
from workflows.builtins import (
    NoopRunner,
    WorkflowSubRunner,
)
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import load_workflow
from workflows.run import WorkflowRunStatus

if TYPE_CHECKING:
    from pathlib import Path

    from workflows.events import WorkflowEvent


# ---------------------------------------------------------------------------
# Harness — mirrors tests/component/test_workflow_sub_runner.py::_run_parent,
# with an injectable event_observer.
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


def _build_registry(
    *,
    catalog: AssetCatalog,
    store: Any,
) -> RunnerRegistry:
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/noop", NoopRunner())

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


def _build_parent_executor(
    tmp_path: Path,
    *,
    parent_yaml: str,
    parent_ref: str,
    children: dict[str, str],
    event_observer: Any = None,
) -> tuple[IterativeExecutor, Any]:
    from workflows.run_store import WorkflowRunStore

    parent_path = tmp_path / "bundles" / "demo" / "workflows" / "parent.yaml"
    _write_workflow(parent_path, parent_yaml)
    assets: dict[str, AssetEntry] = {parent_ref: _entry(parent_ref, parent_path)}
    for ref, content in children.items():
        bundle, _kind, name = ref.split("/")
        child_path = tmp_path / "bundles" / bundle / "workflows" / f"{name}.yaml"
        _write_workflow(child_path, content)
        assets[ref] = _entry(ref, child_path)
    catalog = AssetCatalog(assets=assets)
    store = WorkflowRunStore(tmp_path)
    parent_run = store.create(workflow_ref=parent_ref, inputs={})
    parent_workflow = load_workflow(parent_path)
    registry = _build_registry(catalog=catalog, store=store)
    executor = IterativeExecutor(
        workflow=parent_workflow,
        run=parent_run,
        store=store,
        registry=registry,
        catalog=catalog,
        event_observer=event_observer,
    )
    return executor, parent_run


_PARENT_YAML = """
nodes:
  sub:
    type: kolsch/nodes/workflow
    ref: demo/workflows/child
"""

_CHILD_YAML = """
nodes:
  only:
    type: kolsch/nodes/noop
graph:
  edges: []
"""


# ---------------------------------------------------------------------------
# T1 — child executor emits to the parent's observer.
# ---------------------------------------------------------------------------


async def test_child_events_reach_the_parent_event_observer(tmp_path: Path) -> None:
    captured: list[WorkflowEvent] = []

    executor, parent_run = _build_parent_executor(
        tmp_path,
        parent_yaml=_PARENT_YAML,
        parent_ref="demo/workflows/parent",
        children={"demo/workflows/child": _CHILD_YAML},
        event_observer=captured.append,
    )
    status = await executor.run()
    assert status == WorkflowRunStatus.SUCCEEDED

    parent_run_id = str(parent_run.run_id)
    seen_run_ids = {str(e.workflow_run_id) for e in captured}

    # The parent's own run id is present...
    assert parent_run_id in seen_run_ids
    # ...and at least one OTHER run id — the child's — reached the SAME
    # observer, which is the whole point of spec-103.
    child_run_ids = seen_run_ids - {parent_run_id}
    assert child_run_ids, (
        "no child run_id reached the observer — the child executor was "
        "built without the parent's event_observer"
    )

    # The child's lifecycle events specifically reached the observer.
    child_run_id = next(iter(child_run_ids))
    child_kinds = {
        str(e.kind) for e in captured if str(e.workflow_run_id) == child_run_id
    }
    assert "workflow.run.started" in child_kinds
    assert "workflow.run.completed" in child_kinds
    assert "workflow.node.started" in child_kinds


# ---------------------------------------------------------------------------
# T2 — NodeContext carries the observer handles.
# ---------------------------------------------------------------------------


async def test_node_context_carries_observers_when_set(tmp_path: Path) -> None:
    sentinel_calls: list[Any] = []

    def _observer(event: WorkflowEvent) -> None:
        sentinel_calls.append(event)

    executor, _ = _build_parent_executor(
        tmp_path,
        parent_yaml=_PARENT_YAML,
        parent_ref="demo/workflows/parent",
        children={"demo/workflows/child": _CHILD_YAML},
        event_observer=_observer,
    )
    # `_build_ctx` is the NodeContext factory; build a context for an
    # arbitrary invocation and assert it carries the executor's observer.
    from uuid import uuid4

    from workflows.ids import InvocationId

    node_def = executor._workflow.nodes["sub"]
    ctx = executor._build_ctx(
        inv_id=InvocationId(f"inv_{uuid4()}"),
        node_def=node_def,
        node_name="sub",
    )
    assert ctx.event_observer is _observer
    # The sub-agent observer defaults to None on this executor (none was
    # wired), so the context mirrors that.
    assert ctx.sub_agent_event_observer is None


async def test_node_context_observers_none_when_executor_has_none(
    tmp_path: Path,
) -> None:
    executor, _ = _build_parent_executor(
        tmp_path,
        parent_yaml=_PARENT_YAML,
        parent_ref="demo/workflows/parent",
        children={"demo/workflows/child": _CHILD_YAML},
        event_observer=None,
    )
    from uuid import uuid4

    from workflows.ids import InvocationId

    node_def = executor._workflow.nodes["sub"]
    ctx = executor._build_ctx(
        inv_id=InvocationId(f"inv_{uuid4()}"),
        node_def=node_def,
        node_name="sub",
    )
    assert ctx.event_observer is None
    assert ctx.sub_agent_event_observer is None
