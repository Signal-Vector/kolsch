"""feat-127 Test 1 — `/new` cancels an approval-paused workflow.

Drives a REAL approval park under a `ChatApp` Pilot: the mock executor
blocks on a live `PromptsImpl.choice(...)` with a workflow-scoped source
(the `block_on` mode — no registry-stuffing), so the run sits exactly
where the headline bug lived — `phase="running"`, coroutine parked on a
bare await inside the shared FIFO-drain task. `/new` must NOT refuse: it
retires the run (bug-128's `cancel()` drives `WIRE_PROMPT_CANCELLED` →
adapter → `cancel_pending` → the park unwinds), completes the switch,
and posts the honest "Cancelled 1" notice.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest

from cli.chat.widgets.notice import NoticeWidget
from tests.cli.chat.services.mocks import (
    MockExecutorSpec,
    install_mock_executor_factory,
)
from tests.component.cli.chat.test_new_session_switch import _bootstrap_app
from workflows.events import WorkflowRunStartedEvent
from workflows.ids import TraceId, WorkflowRunId

if TYPE_CHECKING:
    from pathlib import Path


def _started_only() -> list[Any]:
    rid = WorkflowRunId(f"wrk_{uuid4()}")
    return [
        WorkflowRunStartedEvent(workflow_run_id=rid, trace_id=TraceId(f"tr_{uuid4()}"))
    ]


@pytest.mark.asyncio
async def test_new_cancels_approval_paused_workflow(tmp_path: Path) -> None:
    app, _store = await _bootstrap_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        reg = app._service_registry
        assert reg is not None
        wf = cast("Any", reg.workflows)
        prompts = cast("Any", reg.prompts)

        # The mock run parks on a REAL in-node approval: a live
        # `prompts.choice` whose source carries the on-disk run id (the
        # id space `cancel()`'s pending-prompt lookup matches on).
        async def _park(on_disk_run_id: str) -> Any:
            return await prompts.choice(
                "Approve write_file?",
                ["y", "a", "n", "d"],
                source={
                    "kind": "workflow",
                    "run_id": on_disk_run_id,
                    "node_name": "write_reqs",
                },
            )

        install_mock_executor_factory(
            wf,
            {
                "kolsch/workflows/paused": MockExecutorSpec(
                    events=_started_only(),
                    block_on=_park,
                ),
            },
            runs_root=tmp_path / "runs",
        )
        rid = await wf.start("kolsch/workflows/paused", {})

        # Wait until the approval is genuinely parked (pending prompt
        # live + the run registered running).
        for _ in range(150):
            await asyncio.sleep(0.02)
            if prompts.pending_prompt_id is not None:
                break
        assert prompts.pending_prompt_id is not None, "approval never parked"
        record = wf._workflow_registry[rid]
        assert record.phase == "running"
        old_ses = app.session.session_id

        await app._open_new_session()
        await pilot.pause()

        # The switch happened — no refusal.
        assert app.session.session_id != old_ses
        # The parked run was unwound to terminal `cancelled` (the record
        # object outlives the registry clear performed by the switch).
        assert record.phase == "cancelled"
        # The park itself was released — no pending prompt survives.
        assert prompts.pending_prompt_id is None

        notices = [n.text for n in app.query(NoticeWidget)]
        assert not any("can't start a new session" in t for t in notices)
        assert any("Cancelled 1 in-progress workflow." in t for t in notices)
