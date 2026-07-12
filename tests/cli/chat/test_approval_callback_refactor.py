"""spec-004 Test 5 — `InlineApprovalCallback` refactor.

Three passes:

1. Instantiate `InlineApprovalCallback(app, prompts=<mock>)`. Invoke
   with `execution_context=None`; assert the mock's `choice` was
   called with expected question / options / tool-scoped source.
2. Mock returns `"y"`; assert the callback returns
   `AuthzEffect.ALLOW`.
3. Callback no longer imports `ApprovalBlock` — grep the file.
4. Workflow-scoped: when the `execution_context` carries a
   `workflow_run_id`, the derived source is `kind="workflow"`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cli.chat.approval import InlineApprovalCallback
from engine.context import ExecutionContext
from providers.authz import AuthzEffect
from schemas.identifiers import ThreadId
from tools.services.prompts import PromptSource
from workflows.ids import WorkflowRunId


class _MockApp:
    def __init__(self) -> None:
        self.cleared_calls = 0

    def clear_awaiting_approval(self) -> None:
        self.cleared_calls += 1


def _mk_ctx(**overrides: Any) -> ExecutionContext:
    base = {
        "thread_id": ThreadId(
            "th_00000000-0000-0000-0000-000000000001",
        ),
        "trigger": {"kind": "user_message", "content": {}},
    }
    base.update(overrides)
    return ExecutionContext(**base)


async def test_callback_calls_prompts_choice_with_tool_source() -> None:
    app = _MockApp()
    mock_prompts = AsyncMock()
    mock_prompts.choice = AsyncMock(return_value="y")

    cb = InlineApprovalCallback(app, mock_prompts)  # type: ignore[arg-type]

    result = await cb(
        rule_id="rl_x",
        action="bash",
        target="ls",
        caller_chain=("ag_test",),
        execution_context=None,
    )

    assert result is AuthzEffect.ALLOW
    assert mock_prompts.choice.await_count == 1
    call = mock_prompts.choice.await_args
    # question is a format string; options are the canonical set;
    # source is tool-scoped when no execution_context is provided.
    assert call.args[0].startswith("Tool approval")
    assert call.args[1] == ["y", "a", "n", "d"]
    assert call.kwargs["source"]["kind"] == "tool"


async def test_callback_translates_y_to_allow() -> None:
    app = _MockApp()
    mock_prompts = AsyncMock()
    mock_prompts.choice = AsyncMock(return_value="y")
    cb = InlineApprovalCallback(app, mock_prompts)  # type: ignore[arg-type]

    result = await cb(rule_id=None, action="tool", target=None)
    assert result is AuthzEffect.ALLOW


async def test_callback_translates_n_to_deny() -> None:
    app = _MockApp()
    mock_prompts = AsyncMock()
    mock_prompts.choice = AsyncMock(return_value="n")
    cb = InlineApprovalCallback(app, mock_prompts)  # type: ignore[arg-type]

    result = await cb(rule_id=None, action="tool", target=None)
    assert result is AuthzEffect.DENY


async def test_callback_does_not_import_approval_block() -> None:
    """Grep the source: no import of `ApprovalBlock` or widget access.

    The refactor's contract is that the callback no longer touches
    the widget directly — the adapter mounts it via the event
    dispatch path. Documentation references (docstring) are allowed
    since they're the only mention that survives; code references
    must be absent.
    """
    approval_py = (
        Path(__file__).parent.parent.parent.parent
        / "src"
        / "cli"
        / "chat"
        / "approval.py"
    )
    import ast

    tree = ast.parse(approval_py.read_text())
    # Collect the module's runtime-callable names for import and
    # attribute-access checks; docstrings live on the `ast.Expr`
    # nodes that carry `ast.Constant` string values, which we skip.
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = {alias.name for alias in node.names}
            assert "ApprovalBlock" not in names
            assert node.module != "cli.chat.widgets.approval_block"
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "cli.chat.widgets.approval_block"
        if isinstance(node, ast.Attribute):
            # Guard: `self._app.mount_approval_block(...)` would appear
            # as an Attribute — we forbid it here since the widget mount
            # is now the adapter's concern.
            assert node.attr != "mount_approval_block"
            # r-review r1: the pre-existing `get_event_loop` violation
            # is gone; runtime attribute access must be
            # `get_running_loop`.
            assert node.attr != "get_event_loop"

    # And the running-loop accessor is present.
    text = approval_py.read_text()
    assert "get_running_loop" in text


async def test_derive_source_workflow_scoped() -> None:
    app = _MockApp()
    mock_prompts = AsyncMock()
    mock_prompts.choice = AsyncMock(return_value="n")
    cb = InlineApprovalCallback(app, mock_prompts)  # type: ignore[arg-type]

    run_id = WorkflowRunId(
        "wrk_00000000-0000-0000-0000-000000000042",
    )
    ctx = _mk_ctx(workflow_run_id=run_id, workflow_node_name="node_a")

    await cb(
        rule_id=None,
        action="tool",
        target=None,
        execution_context=ctx,
    )
    source: PromptSource = mock_prompts.choice.await_args.kwargs["source"]
    assert source["kind"] == "workflow"
    assert source["run_id"] == str(run_id)
    assert source["node_name"] == "node_a"
    assert source["run_chain"] == []


async def test_clear_awaiting_approval_called_on_exit() -> None:
    app = _MockApp()
    mock_prompts = AsyncMock()
    mock_prompts.choice = AsyncMock(return_value="y")
    cb = InlineApprovalCallback(app, mock_prompts)  # type: ignore[arg-type]

    await cb(rule_id=None, action="tool", target=None)
    assert app.cleared_calls == 1
