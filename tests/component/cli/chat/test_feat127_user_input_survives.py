"""feat-127 Test 4 — a `user_input`-suspended run survives `/new` untouched.

A durably-suspended run (`meta.json` status `waiting_for_input` + a
`waiting_at` sidecar) has already RETURNED from the executor — it is off
the live machinery and resumable via `kolsch workflows resume`. `/new`
must NOT cancel it: a `cancel.flag` in its run dir would abort the
resume. The fixture is written to disk directly (per the spec's harness
note — a durably-suspended run can't be produced by the mock executor)
and registered in-memory as `running` to exercise the narrow window
where the registry hasn't caught up with the on-disk pause.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest

from cli.chat.services.workflows_impl import WorkflowRunRecord
from cli.chat.widgets.notice import NoticeWidget
from tests.component.cli.chat.test_new_session_switch import _bootstrap_app
from workflows.ids import WorkflowRunId
from workflows.run import RunDirectory

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_user_input_suspended_run_survives_new(tmp_path: Path) -> None:
    app, _store = await _bootstrap_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        reg = app._service_registry
        assert reg is not None
        wf = cast("Any", reg.workflows)

        # On-disk pause contract, written directly: waiting_at sidecar +
        # meta.json at `waiting_for_input`.
        run_dir = RunDirectory(root=tmp_path / "runs" / f"wrk_{uuid4()}")
        run_dir.root.mkdir(parents=True, exist_ok=True)
        run_dir.waiting_at_path.write_text("inv_feat127", encoding="utf-8")
        run_dir.meta_path.write_text(
            json.dumps({"status": "waiting_for_input"}),
            encoding="utf-8",
        )
        rid = WorkflowRunId(run_dir.root.name)
        wf._workflow_registry[rid] = WorkflowRunRecord(
            run_id=rid,
            ref="kolsch/workflows/suspended",
            phase="running",  # the narrow in-memory-lags-disk window
            card=None,
            created_at=datetime.now(UTC),
            run_dir=run_dir,
        )
        old_ses = app.session.session_id

        await app._open_new_session()
        await pilot.pause()

        # The switch proceeded…
        assert app.session.session_id != old_ses
        # …and the suspended run's disk state is byte-identical:
        # no cancel.flag, still waiting_for_input, sidecar intact.
        assert not run_dir.cancel_flag_path.exists()
        meta = json.loads(run_dir.meta_path.read_text(encoding="utf-8"))
        assert meta["status"] == "waiting_for_input"
        assert run_dir.waiting_at_path.read_text(encoding="utf-8") == "inv_feat127"

        # It was SKIPPED, not counted — no "Cancelled N" notice mounts.
        notices = [n.text for n in app.query(NoticeWidget)]
        assert not any("Cancelled" in t and "workflow" in t for t in notices)
