"""spec-014 Test 1 — `InlineApprovalCallback._derive_source` workflow branch.

Three passes:

1. Workflow context populated → source is `kind="workflow"` with
   `run_id` / `node_name` / `run_chain=[]`.
2. No workflow context → source is `kind="tool"` (spec-004 baseline).
3. `run_chain` is an empty list in v1 (spec-017 populates for nested
   workflows).

Complements `test_approval_callback_refactor.py::test_derive_source_workflow_scoped`
which validates the same branch through the callback surface — this
test hits `_derive_source` directly so failures pinpoint the helper.
"""

from __future__ import annotations

from typing import Any

from cli.chat.approval import InlineApprovalCallback
from engine.context import ExecutionContext
from schemas.identifiers import ThreadId
from workflows.ids import WorkflowRunId


def _mk_ctx(**overrides: Any) -> ExecutionContext:
    base: dict[str, Any] = {
        "thread_id": ThreadId(
            "th_00000000-0000-0000-0000-000000000001",
        ),
        "trigger": {"kind": "user_message", "content": {}},
    }
    base.update(overrides)
    return ExecutionContext(**base)


def test_derive_source_workflow_populated() -> None:
    """Workflow context → `kind='workflow'` with `run_id`/`node_name`."""
    run_id = WorkflowRunId(
        "wrk_00000000-0000-0000-0000-000000000042",
    )
    ctx = _mk_ctx(
        workflow_run_id=run_id,
        workflow_node_name="build",
    )
    source = InlineApprovalCallback._derive_source(ctx)
    assert source["kind"] == "workflow"
    assert source["run_id"] == str(run_id)
    assert source["node_name"] == "build"
    assert source["run_chain"] == []


def test_derive_source_no_workflow_context_tool_scoped() -> None:
    """No workflow_run_id → tool-scoped source (spec-004 baseline)."""
    ctx = _mk_ctx()
    source = InlineApprovalCallback._derive_source(ctx)
    assert source["kind"] == "tool"
    # Empty context (no ExecutionContext at all) also tool-scoped.
    source_none = InlineApprovalCallback._derive_source(None)
    assert source_none["kind"] == "tool"


def test_derive_source_run_chain_empty_for_v1() -> None:
    """`run_chain` is a stable empty list — spec-017 populates."""
    run_id = WorkflowRunId(
        "wrk_00000000-0000-0000-0000-000000000099",
    )
    ctx = _mk_ctx(
        workflow_run_id=run_id,
        workflow_node_name="lint",
    )
    source = InlineApprovalCallback._derive_source(ctx)
    assert source["run_chain"] == []
    assert isinstance(source["run_chain"], list)
