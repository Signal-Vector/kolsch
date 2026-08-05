"""feat-127 Test 2 — no old-run / old-prompt event bleeds into the new thread.

The non-negotiable `/new` invariant: retirement drives EVERY live writer
(per-run bridge drains, the FIFO-drain-hosted `_finalize_terminal` tail,
`PromptsImpl`) to `done()` BEFORE any session id is repointed — else the
old run's/prompt's tail `append_event` lands in the NEW thread.

Exercised with a REAL mid-flight writer: a run parked on a live
`prompts.choice(...)` (the same `block_on` mode as Test 1 — not a stub).
Every `append_event` is spied from before `/new` through a post-switch
settle window; nothing referencing the old run (either id space) or the
old prompt may target the new thread.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest

from tests.cli.chat.services.mocks import (
    MockExecutorSpec,
    install_mock_executor_factory,
)
from tests.component.cli.chat.test_new_session_switch import _bootstrap_app
from workflows.events import WorkflowRunStartedEvent
from workflows.ids import TraceId, WorkflowRunId

if TYPE_CHECKING:
    from pathlib import Path

    from schemas.events import Event


def _started_only() -> list[Any]:
    rid = WorkflowRunId(f"wrk_{uuid4()}")
    return [
        WorkflowRunStartedEvent(workflow_run_id=rid, trace_id=TraceId(f"tr_{uuid4()}"))
    ]


@pytest.mark.asyncio
async def test_no_old_run_or_prompt_write_lands_in_new_thread(
    tmp_path: Path,
) -> None:
    app, store = await _bootstrap_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        reg = app._service_registry
        assert reg is not None
        wf = cast("Any", reg.workflows)
        prompts = cast("Any", reg.prompts)

        async def _park(on_disk_run_id: str) -> Any:
            return await prompts.choice(
                "Approve?",
                ["y", "n"],
                source={
                    "kind": "workflow",
                    "run_id": on_disk_run_id,
                    "node_name": "n1",
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
        for _ in range(150):
            await asyncio.sleep(0.02)
            if prompts.pending_prompt_id is not None:
                break
        assert prompts.pending_prompt_id is not None, "approval never parked"

        old_th = str(app.session.thread_id)
        old_prompt_id = str(prompts.pending_prompt_id)
        on_disk_id = str(wf._workflow_registry[rid].run_dir.root.name)

        # Spy EVERY durable append from here on — the retirement tail,
        # the switch, and the post-switch settle window all flow
        # through this single seam.
        appended: list[tuple[str, Event]] = []
        real_append = store.append_event

        async def _spy_append(thread_id: Any, event: Event) -> Any:
            appended.append((str(thread_id), event))
            return await real_append(thread_id, event)

        store.append_event = _spy_append  # type: ignore[method-assign]

        await app._open_new_session()
        await pilot.pause()
        # Settle window: any straggler writer that survived retirement
        # would fire here (its `finally` emit) — with the ids already
        # repointed at the NEW thread.
        await asyncio.sleep(0.3)
        await pilot.pause()

        new_th = str(app.session.thread_id)
        assert new_th != old_th

        offenders = []
        for thread_id, event in appended:
            if thread_id != new_th:
                continue  # old-thread tail writes during retirement are fine
            blob = json.dumps(event.data, default=str)
            if on_disk_id in blob or str(rid) in blob or old_prompt_id in blob:
                offenders.append(event.data)
        assert not offenders, f"old-run/prompt writes hit the new thread: {offenders}"

        # Belt-and-braces: read the new thread's log back — it contains
        # nothing from the old run (either id space) or the old prompt.
        async for event in store.read_events(app.session.thread_id):
            blob = json.dumps(event.data, default=str)
            assert on_disk_id not in blob
            assert str(rid) not in blob
            assert old_prompt_id not in blob
