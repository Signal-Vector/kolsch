"""bug-153 — pin workflow-tool behaviour that only the shipped copies have.

Until bug-153 the repo carried stale repo-root forks of the butler
workflow tools, and the tool tests loaded those forks. Two fixes had
landed only in the shipped starter copies, so no test exercised them:

- ``run_workflow`` resolves its blocking timeout from
  ``Workflows.default_timeout_seconds`` (sourced from
  ``platform.yaml:workflows.default_timeout_seconds``) when the caller
  omits ``timeout_seconds``. The fork hardcoded 300s.
- ``workflow_status`` returns bug-066's ``on_disk_run_id`` — the id
  that keys ``runs/<id>/`` on disk. The fork dropped it.

Both tests call the tool body directly against minimal duck-typed
mocks, in the style of ``test_run_workflow_error_message_shape.py``.
"""

from __future__ import annotations

import sys
from typing import Any
from uuid import uuid4

import pytest

from schemas.caller_chain import CallerChain
from schemas.identifiers import AgentId, SessionId, ThreadId
from tests.fixtures.bundle_paths import STARTER_TOOLS_DIR, load_bundle_tool
from tools.decorator import ToolContext, get_tool_definition
from tools.services.workflows import WorkflowRunStatusView
from workflows.ids import WorkflowRunId

_TOOLS_DIR = STARTER_TOOLS_DIR

_RUN_ID = f"wrk_{uuid4()}"
_ON_DISK_RUN_ID = f"wrk_{uuid4()}"

_PLATFORM_DEFAULT_TIMEOUT_SECONDS = 1


class _MockCard:
    def __init__(self) -> None:
        self.id = "card_probe"
        self._owner_transferred = False
        self.is_closed = False


class _MockCards:
    async def open(self, label: str, *, kind: str) -> Any:
        _ = (label, kind)
        return _MockCard()


class _NeverTerminalWorkflows:
    """Workflows mock whose run stays ``running`` forever.

    Exposes ``default_timeout_seconds`` the way ``WorkflowsImpl`` does
    when ``platform.yaml:workflows.default_timeout_seconds`` is set.
    """

    def __init__(self, *, default_timeout_seconds: int, max_polls: int) -> None:
        self._default_timeout_seconds = default_timeout_seconds
        self._max_polls = max_polls
        self.status_polls = 0

    @property
    def default_timeout_seconds(self) -> int:
        return self._default_timeout_seconds

    async def start(
        self,
        ref: str,
        inputs: dict[str, Any],
        *,
        card: Any = None,
    ) -> str:
        _ = (ref, inputs, card)
        return _RUN_ID

    def status(self, run_id: Any) -> WorkflowRunStatusView:
        self.status_polls += 1
        if self.status_polls > self._max_polls:
            msg = (
                f"run_workflow polled status {self.status_polls} times; "
                f"it ignored default_timeout_seconds="
                f"{self._default_timeout_seconds}"
            )
            raise AssertionError(msg)
        return WorkflowRunStatusView(run_id=WorkflowRunId(str(run_id)), phase="running")


class _StatusOnlyWorkflows:
    """Workflows mock returning one fixed status view."""

    def __init__(self, view: WorkflowRunStatusView) -> None:
        self._view = view

    def status(self, run_id: Any) -> WorkflowRunStatusView:
        _ = run_id
        return self._view


def _mk_ctx(*, cards: Any = None, workflows: Any = None) -> ToolContext:
    return ToolContext(
        agent_id=AgentId(f"ag_{uuid4()}"),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal="agent.test",
        secrets={},
        cards=cards,
        workflows=workflows,
        caller_chain=CallerChain.unattributed(),
    )


@pytest.mark.asyncio
async def test_run_workflow_uses_platform_default_timeout() -> None:
    """Omitting ``timeout_seconds`` blocks for the platform default, not 300s."""
    run_workflow = load_bundle_tool(_TOOLS_DIR, "run_workflow.py", "run_workflow")
    # Derive the expected poll count from the tool's own interval so the
    # test doesn't pin it. A tool that ignores the platform default and
    # falls back to 300s polls far more often, so the mock fails past a
    # small margin instead of letting it block.
    poll_interval = sys.modules[run_workflow.__module__]._POLL_INTERVAL_SECONDS
    expected_polls = max(1, int(_PLATFORM_DEFAULT_TIMEOUT_SECONDS / poll_interval))
    workflows = _NeverTerminalWorkflows(
        default_timeout_seconds=_PLATFORM_DEFAULT_TIMEOUT_SECONDS,
        max_polls=expected_polls + 5,
    )
    ctx = _mk_ctx(cards=_MockCards(), workflows=workflows)

    defn = get_tool_definition(run_workflow)
    args = defn.args_schema.model_validate({"ref": "acme/workflows/slow", "inputs": {}})
    result = await defn.func(args, ctx)

    assert result["phase"] == "running"
    assert result["run_id"] == _RUN_ID
    assert result["note"].startswith("Workflow still running after 1 seconds;")
    assert workflows.status_polls == expected_polls


@pytest.mark.asyncio
async def test_workflow_status_returns_on_disk_run_id() -> None:
    """``workflow_status`` surfaces bug-066's canonical on-disk run id."""
    workflow_status = load_bundle_tool(
        _TOOLS_DIR, "workflow_status.py", "workflow_status"
    )
    view = WorkflowRunStatusView(
        run_id=WorkflowRunId(_RUN_ID),
        phase="running",
        ref="acme/workflows/slow",
        on_disk_run_id=WorkflowRunId(_ON_DISK_RUN_ID),
    )
    ctx = _mk_ctx(workflows=_StatusOnlyWorkflows(view))

    defn = get_tool_definition(workflow_status)
    args = defn.args_schema.model_validate({"run_id": _RUN_ID})
    result = await defn.func(args, ctx)

    assert result["run_id"] == _RUN_ID
    assert result["on_disk_run_id"] == _ON_DISK_RUN_ID
