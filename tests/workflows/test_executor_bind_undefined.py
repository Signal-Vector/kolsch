"""bug-062 B2 — runtime guard for fired-edge binds that render to Undefined.

Covers acceptance criteria 6, 7, 8, 9:

- Test 1: a fired-edge bind that renders to a Jinja ``Undefined``
  raises :class:`WorkflowBindUndefined` naming the edge, key, and
  expression — NOT the opaque
  ``Unable to serialize unknown type: ...Undefined`` crash. (AC #6/#7)
- Test 2: pruned-edge Undefined semantics are preserved — a
  branch/prune workflow where a suppressed edge's bind key is
  referenced by downstream node-config Jinja still runs. (AC #8)
- Test 3: the failing run's terminal events serialize cleanly (no
  ``jinja2.runtime.Undefined`` in any emitted payload). (AC #9)

The B2 guard is scoped to open node types (whose output schema the
validator can't see statically). Sub-workflow / authored-Python
nodes fall into this bucket; agent nodes with an unresolvable ref
also degrade to open at validate time (bug-062 AC #4) and land in the
runtime guard as a safety net.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from assets.catalog import AssetCatalog
from workflows.errors import WorkflowBindUndefined
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    catalog: AssetCatalog | None = None,
) -> tuple[IterativeExecutor, WorkflowRunStore, Any]:
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=catalog if catalog is not None else AssetCatalog(),
    )
    return ex, store, run


def _bind_undefined_workflow() -> Workflow:
    """Build the canonical bug-062 fixture.

    A two-node linear workflow whose source is a ``kolsch/nodes/agent``
    node with an UNRESOLVABLE agent ref (so the B1 validator falls
    back to open per AC #4) whose runner returns ``{"text": "hi"}``.
    The receiver binds ``nodes.<src>.indexed_files`` which the source
    does not expose — Jinja renders that to a silent ``Undefined``.

    Without B2 the ``Undefined`` object would be stored as the
    receiver's bound input and later crash JSON serialization with
    the opaque ``Unable to serialize unknown type: ...Undefined``
    message; with B2 the executor raises
    :class:`WorkflowBindUndefined` at bind-render time.
    """
    return _wf(
        {
            "nodes": {
                "src": {
                    "type": "kolsch/nodes/agent",
                    "agent": "demo/agents/mystery",  # unresolvable
                },
                "sink": {
                    "type": "kolsch/nodes/agent",
                    "agent": "demo/agents/receiver",  # unresolvable
                },
            },
            "graph": {
                "edges": [
                    {
                        "from": "src",
                        "to": "sink",
                        "binds": {
                            "indexed_files": (
                                "{{ nodes.src.indexed_files }}"
                            ),
                        },
                    }
                ]
            },
        }
    )


# ---------------------------------------------------------------------------
# Test 1 — WorkflowBindUndefined replaces the opaque serialization crash
# ---------------------------------------------------------------------------


async def test_bind_undefined_raises_workflow_bind_undefined(
    tmp_path: Path,
) -> None:
    """A fired-edge bind that renders to Undefined MUST surface via
    :class:`WorkflowBindUndefined` naming the edge, bind key, and
    expression — NOT the opaque
    ``Unable to serialize unknown type: <class 'jinja2.runtime.Undefined'>``
    crash the pre-bug-062 code produced."""
    wf = _bind_undefined_workflow()
    registry = RunnerRegistry()
    # Source runner returns `{"text": "hi"}` (no `indexed_files` key);
    # `nodes.src.indexed_files` therefore renders to Undefined.
    registry.register("kolsch/nodes/agent", StaticRunner({"text": "hi"}))
    ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)

    status = await ex.run()
    assert status == WorkflowRunStatus.FAILED, (
        f"expected FAILED terminal (bind Undefined guard should fire); "
        f"got {status!r}"
    )

    events = list(store.read_events(run.run_id))
    kinds = [e["kind"] for e in events]
    # Expect the terminal chain: run.started ... node.failed, run.failed
    assert "workflow.node.failed" in kinds, kinds
    assert "workflow.run.failed" in kinds, kinds
    # The failed event must carry the WorkflowBindUndefined
    # error class + a message that identifies edge/key/expression —
    # NOT the opaque serialization crash text.
    failed = next(e for e in events if e["kind"] == "workflow.node.failed")
    payload = failed["payload"]
    assert payload["error_class"] == "WorkflowBindUndefined", payload
    message = payload["message"]
    assert "'src->sink'" in message, message
    assert "'indexed_files'" in message, message
    assert "nodes.src.indexed_files" in message, message
    assert "Unable to serialize unknown type" not in message, message


async def test_bind_undefined_type_class_is_workflow_bind_undefined() -> None:
    """Pin the class identity: :class:`WorkflowBindUndefined` is a
    ``WorkflowRuntimeError`` subclass (a runtime failure, not a
    validation error)."""
    from workflows.errors import WorkflowError, WorkflowRuntimeError

    exc = WorkflowBindUndefined(
        from_node="a",
        to_node="b",
        bind_key="k",
        expression="{{ nodes.a.missing }}",
        source_fields=("text",),
    )
    assert isinstance(exc, WorkflowRuntimeError)
    assert isinstance(exc, WorkflowError)
    msg = str(exc)
    assert "'a->b'" in msg
    assert "'k'" in msg
    assert "nodes.a.missing" in msg
    assert "text" in msg


# ---------------------------------------------------------------------------
# Test 2 — pruned-edge Undefined semantics preserved (AC #8)
# ---------------------------------------------------------------------------


async def test_pruned_edge_undefined_in_node_config_still_works(
    tmp_path: Path,
) -> None:
    """A branch node prunes the false arm. A downstream diamond-tail
    node's bash ``command:`` references a bind key that was ONLY
    bound on the pruned edge — under Jinja's silent Undefined that
    key resolves to Undefined in the *node-config* render (not the
    fired-edge bind render), and the workflow must complete without
    :class:`WorkflowBindUndefined` firing. This pins that the B2
    guard is scoped to fired-edge bind rendering ONLY (not
    node-config rendering)."""
    # Diamond:
    #   decider(branch)
    #     -true->  arm_t --> tail
    #     -false-> arm_f --> tail (pruned when decider is true)
    # Edge arm_t -> tail binds `taken_val`; edge arm_f -> tail binds
    # `not_taken_val`. When decider fires with True, the arm_f -> tail
    # edge is pruned so `not_taken_val` is NOT in tail's bound inputs.
    # `tail`'s bash `command:` references `{{ inputs.not_taken_val }}`
    # (node-config render), which resolves to silent Undefined per
    # the intentional pruned-edge semantics — MUST remain silent.
    wf = _wf(
        {
            "nodes": {
                "decider": {
                    "type": "kolsch/nodes/branch",
                    "condition": "{{ true }}",
                },
                "arm_t": {"type": "kolsch/nodes/bash", "command": "echo t"},
                "arm_f": {"type": "kolsch/nodes/bash", "command": "echo f"},
                "tail": {
                    "type": "kolsch/nodes/bash",
                    "command": "echo {{ inputs.not_taken_val }}",
                },
            },
            "graph": {
                "edges": [
                    {"from": "decider", "to": "arm_t", "branch": True},
                    {"from": "decider", "to": "arm_f", "branch": False},
                    {
                        "from": "arm_t",
                        "to": "tail",
                        "binds": {"taken_val": "ok"},
                    },
                    {
                        "from": "arm_f",
                        "to": "tail",
                        "binds": {"not_taken_val": "never"},
                    },
                ]
            },
        }
    )
    registry = RunnerRegistry()
    registry.register(
        "kolsch/nodes/bash",
        StaticRunner({"stdout": "ok", "stderr": "", "exit_code": 0}),
    )
    from workflows.builtins.branch import BranchRunner

    registry.register("kolsch/nodes/branch", BranchRunner())
    ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)

    status = await ex.run()
    events = list(store.read_events(run.run_id))
    kinds = [e["kind"] for e in events]
    assert status == WorkflowRunStatus.SUCCEEDED, (
        f"pruned-edge Undefined must remain silent; got status={status!r}, "
        f"kinds={kinds}"
    )
    # And no `WorkflowBindUndefined` was raised anywhere.
    for e in events:
        payload = e.get("payload") or {}
        assert payload.get("error_class") != "WorkflowBindUndefined", (
            f"unexpected WorkflowBindUndefined in event {e}"
        )


# ---------------------------------------------------------------------------
# Test 3 — failing run emits a serializable terminal (AC #9)
# ---------------------------------------------------------------------------


async def test_bind_undefined_terminal_events_serialize(tmp_path: Path) -> None:
    """After the B2 guard fires, the emitted ``workflow.node.failed``
    and ``workflow.run.failed`` payloads MUST serialize cleanly — no
    ``jinja2.runtime.Undefined`` leaks into any emitted payload. This
    is the emit-side root cause of the ``ses_98f7397c…`` ticking-timer
    symptom: because the failing terminal now serializes, the chat
    card's timer stops (see bug-062 AC #9 and bug-063 for the card
    presentation)."""
    wf = _bind_undefined_workflow()
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/agent", StaticRunner({"text": "hi"}))
    ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)

    status = await ex.run()
    assert status == WorkflowRunStatus.FAILED

    events = list(store.read_events(run.run_id))
    # Every emitted event's payload MUST json-serialize without
    # raising, and must contain no reference to Jinja Undefined.
    for e in events:
        payload = e.get("payload") or {}
        try:
            raw = json.dumps(payload)
        except (TypeError, ValueError) as exc:
            pytest.fail(
                f"payload for event {e['kind']!r} failed to serialize: "
                f"{exc!r}"
            )
        assert "jinja2.runtime.Undefined" not in raw, (
            f"event {e['kind']!r} payload leaked Jinja Undefined: {raw}"
        )
    # Explicitly assert the failing terminal pair is present + clean.
    kinds = [e["kind"] for e in events]
    assert kinds[-1] == "workflow.run.failed", kinds
    assert "workflow.node.failed" in kinds, kinds
