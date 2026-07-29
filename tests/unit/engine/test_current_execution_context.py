"""bug-107 Test 5 — `_current_execution_context` set/reset ordering.

The engine publishes the running exchange's `ExecutionContext` into a
task-local `contextvars.ContextVar` so the approval callback
(`InlineApprovalCallback._derive_source`, reached deep in the tool-
dispatch chain) can tag its `kolsch.prompt.requested` source as
`kind="workflow"` when the invoking tool runs under a workflow-scoped
exchange. `_Exchange.run` sets the token on entry and resets it on the
finally — mirroring the existing `_current_exchange_span_registry`
pattern at `src/engine/engine.py:136-157`.

Under intra-task nesting (an outer exchange whose driver spawns a
nested exchange on the same task — the shape a sub-workflow agent
node produces when it calls back into `engine.execute_stream`), the
inner `set(B)` shadows the outer `set(A)`; `reset(token_B)` must
restore `A`, and `reset(token_A)` must restore `None`. Bare
`.set(None)` would flatten the stack — hence the Token-based semantics.

This test validates the ordering directly. Asyncio's copy-on-fork of
contextvars into each spawned task already protects sibling tasks from
leak — that's a runtime guarantee, not what we're testing.
"""

from __future__ import annotations

from typing import Any

from engine.context import ExecutionContext
from engine.engine import (
    _current_execution_context,
    current_execution_context,
)
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


def test_current_execution_context_nested_set_reset_restores_stack() -> None:
    """Nested `set(A)/set(B)/reset(B)/reset(A)` yields `None→A→B→A→None`.

    Mirrors `_Exchange.run`'s Token-based `set`/`reset` shape at
    `src/engine/engine.py`. A workflow-scoped outer exchange with an
    inner sub-agent exchange on the same task must see the inner
    ExecutionContext during the inner scope, then transparently see the
    outer one again after the inner resets — the ordering guarantee the
    approval callback relies on for correctly-tagged sources.
    """
    ctx_a = _mk_ctx(
        workflow_run_id=WorkflowRunId(
            "wrk_00000000-0000-0000-0000-000000000001",
        ),
        workflow_node_name="outer",
    )
    ctx_b = _mk_ctx(
        workflow_run_id=WorkflowRunId(
            "wrk_00000000-0000-0000-0000-000000000002",
        ),
        workflow_node_name="inner",
    )

    # Baseline — no exchange on the stack.
    assert current_execution_context() is None

    token_a = _current_execution_context.set(ctx_a)
    try:
        # Outer set — reader sees A.
        assert current_execution_context() is ctx_a

        token_b = _current_execution_context.set(ctx_b)
        try:
            # Inner set — reader sees B (A is shadowed, not lost).
            assert current_execution_context() is ctx_b
        finally:
            _current_execution_context.reset(token_b)

        # Inner reset — reader sees A again.
        assert current_execution_context() is ctx_a
    finally:
        _current_execution_context.reset(token_a)

    # Outer reset — back to baseline.
    assert current_execution_context() is None
