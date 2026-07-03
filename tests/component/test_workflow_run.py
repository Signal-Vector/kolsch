"""Phase 3.1 Step 0 — workflow run lifecycle tests.

Covers ACs 0.1-0.8:

- :class:`WorkflowRunStatus` exposes all 7 values + a ``terminal_set``.
- :class:`WorkflowRun` carries the spec-named fields.
- :meth:`WorkflowRunStore.create` mints a fresh ``wrk_*`` id + creates
  the run directory.
- :meth:`WorkflowRunStore.append_event` + ``read_events`` round-trip.
- :meth:`WorkflowRunStore.list` orders runs by ``created_at`` desc.
- :meth:`WorkflowRunStore.tail` polls until the terminal event lands.
- ``read`` lifts the status from terminal ``workflow.run.*`` events
  in the JSONL tail.
- :class:`RunDirectory` computes the three sidecar paths.
- :mod:`workflows.module_isolation` honours the bundles-only filter —
  third-party packages survive cleanup.
"""

from __future__ import annotations

import importlib
import json
import sys
import textwrap
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from workflows.ids import TraceId, WorkflowRunId
from workflows.module_isolation import (
    cleanup_bundle_imports,
    snapshot_sys_modules,
)
from workflows.run import RunDirectory, WorkflowRun, WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

# ---------------------------------------------------------------------------
# AC 0.1 — WorkflowRunStatus
# ---------------------------------------------------------------------------


class TestStatusEnum:
    def test_all_seven_values_present(self) -> None:
        names = {s.name for s in WorkflowRunStatus}
        assert names == {
            "PENDING",
            "RUNNING",
            "WAITING_FOR_INPUT",
            "SUCCEEDED",
            "FAILED",
            "TIMED_OUT",
            "ABORTED",
        }

    def test_terminal_set(self) -> None:
        terminal = WorkflowRunStatus.terminal_set()
        assert terminal == {
            WorkflowRunStatus.SUCCEEDED,
            WorkflowRunStatus.FAILED,
            WorkflowRunStatus.TIMED_OUT,
            WorkflowRunStatus.ABORTED,
        }


# ---------------------------------------------------------------------------
# AC 0.2 — WorkflowRun model
# ---------------------------------------------------------------------------


class TestRunModel:
    def test_model_fields_match_spec(self) -> None:
        run = WorkflowRun(
            run_id=WorkflowRunId("wrk_5a16f7f0-3c66-4d3d-b6cd-3d39f88a4cba"),
            trace_id=TraceId("tr_8c5e1c08-2a16-4d1e-95ab-0d0a8e21a6b9"),
            workflow_ref="demo/workflows/hello",
            inputs={"name": "Ada"},
            created_at=datetime.now(UTC),
        )
        assert run.status == WorkflowRunStatus.PENDING
        assert run.pid is None
        assert run.reason is None
        assert run.workflow_ref == "demo/workflows/hello"
        assert run.inputs == {"name": "Ada"}


# ---------------------------------------------------------------------------
# AC 0.3 / 0.4 — WorkflowRunStore CRUD + JSONL round-trip
# ---------------------------------------------------------------------------


class TestStoreRoundTrip:
    def test_create_mints_fresh_id_and_writes_meta(self, tmp_path: Path) -> None:
        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/hello", inputs={"x": 1})
        assert run.run_id.startswith("wrk_")
        assert run.trace_id.startswith("tr_")
        assert (tmp_path / "runs" / str(run.run_id) / "meta.json").exists()

    def test_create_accepts_caller_supplied_trace_id(self, tmp_path: Path) -> None:
        trace = TraceId("tr_4b1cf4c6-2c0c-4b6a-913f-f3b9b4ab2c12")
        store = WorkflowRunStore(tmp_path)
        run = store.create(
            workflow_ref="demo/workflows/hello",
            inputs={},
            trace_id=trace,
        )
        assert run.trace_id == trace

    def test_append_and_read_events_round_trip(self, tmp_path: Path) -> None:
        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/hello", inputs={})
        events = [
            {"kind": "workflow.run.started", "payload": {}},
            {"kind": "workflow.node.started", "payload": {"node_name": "a"}},
            {"kind": "workflow.node.completed", "payload": {"node_name": "a"}},
            {"kind": "workflow.run.completed", "payload": {}},
        ]
        for evt in events:
            store.append_event(run.run_id, evt)
        read = list(store.read_events(run.run_id))
        assert read == events

    def test_read_events_empty_when_no_events_yet(self, tmp_path: Path) -> None:
        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/hello", inputs={})
        assert list(store.read_events(run.run_id)) == []

    def test_read_events_raises_for_unknown_run(self, tmp_path: Path) -> None:
        store = WorkflowRunStore(tmp_path)
        bogus = WorkflowRunId("wrk_aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa")
        with pytest.raises(FileNotFoundError):
            list(store.read_events(bogus))


# ---------------------------------------------------------------------------
# read lifts terminal status from JSONL
# ---------------------------------------------------------------------------


class TestReadLiftsStatus:
    def test_terminal_event_drives_status(self, tmp_path: Path) -> None:
        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/hello", inputs={})
        store.append_event(run.run_id, {"kind": "workflow.run.started", "payload": {}})
        store.append_event(
            run.run_id, {"kind": "workflow.run.completed", "payload": {}}
        )
        reread = store.read(run.run_id)
        assert reread.status == WorkflowRunStatus.SUCCEEDED

    def test_aborted_event_includes_reason(self, tmp_path: Path) -> None:
        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/hello", inputs={})
        store.append_event(
            run.run_id,
            {
                "kind": "workflow.run.aborted",
                "payload": {"reason": "user_cancel"},
            },
        )
        reread = store.read(run.run_id)
        assert reread.status == WorkflowRunStatus.ABORTED
        assert reread.reason == "user_cancel"

    def test_no_terminal_event_preserves_meta_status(self, tmp_path: Path) -> None:
        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/hello", inputs={})
        # Only a started event — no terminal yet.
        store.append_event(run.run_id, {"kind": "workflow.run.started", "payload": {}})
        reread = store.read(run.run_id)
        assert reread.status == WorkflowRunStatus.PENDING


# ---------------------------------------------------------------------------
# list ordering
# ---------------------------------------------------------------------------


class TestListOrdering:
    def test_list_orders_newest_first(self, tmp_path: Path) -> None:
        store = WorkflowRunStore(tmp_path)
        older = store.create(workflow_ref="demo/workflows/hello", inputs={})
        # Force the second run's created_at to be strictly later.
        time.sleep(0.01)
        newer = store.create(workflow_ref="demo/workflows/hello", inputs={})
        rows = store.list()
        assert [r.run_id for r in rows] == [newer.run_id, older.run_id]

    def test_list_returns_empty_when_no_runs(self, tmp_path: Path) -> None:
        store = WorkflowRunStore(tmp_path)
        assert store.list() == []


# ---------------------------------------------------------------------------
# AC 0.5 — tail polls until terminal
# ---------------------------------------------------------------------------


class TestTail:
    def test_tail_yields_events_then_terminates_on_terminal(
        self, tmp_path: Path
    ) -> None:
        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/hello", inputs={})

        produced: list[dict] = []

        def producer() -> None:
            # Stagger the writes so the consumer observes them via polling.
            time.sleep(0.05)
            store.append_event(
                run.run_id, {"kind": "workflow.run.started", "payload": {}}
            )
            time.sleep(0.05)
            store.append_event(
                run.run_id,
                {"kind": "workflow.node.started", "payload": {"n": "a"}},
            )
            time.sleep(0.05)
            store.append_event(
                run.run_id, {"kind": "workflow.run.completed", "payload": {}}
            )

        thread = threading.Thread(target=producer)
        thread.start()
        try:
            for evt in store.tail(run.run_id, poll_seconds=0.02):
                produced.append(evt)
        finally:
            thread.join(timeout=2.0)

        assert [e["kind"] for e in produced] == [
            "workflow.run.started",
            "workflow.node.started",
            "workflow.run.completed",
        ]


# ---------------------------------------------------------------------------
# RunDirectory
# ---------------------------------------------------------------------------


class TestRunDirectory:
    def test_paths_compose_correctly(self, tmp_path: Path) -> None:
        d = RunDirectory(root=tmp_path / "runs" / "wrk_xyz")
        assert d.events_path == tmp_path / "runs" / "wrk_xyz" / "events.jsonl"
        assert d.pid_path == tmp_path / "runs" / "wrk_xyz" / "pid"
        assert d.cancel_flag_path == tmp_path / "runs" / "wrk_xyz" / "cancel.flag"
        assert d.meta_path == tmp_path / "runs" / "wrk_xyz" / "meta.json"


# ---------------------------------------------------------------------------
# AC 0.6 — module isolation
# ---------------------------------------------------------------------------


class TestModuleIsolation:
    def test_bundle_imports_removed_third_party_survives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Lay down a fake bundle Python file the test can import.
        bundle_dir = tmp_path / "bundles" / "demo" / "nodes"
        bundle_dir.mkdir(parents=True)
        (bundle_dir / "echo.py").write_text("MARKER = 42\n", encoding="utf-8")
        # Add the workspace root to sys.path so importlib can find it.
        monkeypatch.syspath_prepend(str(tmp_path))

        before = snapshot_sys_modules()
        # Verify pydantic is loaded BEFORE the snapshot (it should be —
        # the kolsch package imports it transitively at test start).
        assert "pydantic" in before

        # Now import the bundle module.
        module = importlib.import_module("bundles.demo.nodes.echo")
        assert getattr(module, "MARKER", None) == 42
        assert "bundles.demo.nodes.echo" in sys.modules

        removed = cleanup_bundle_imports(before=before, workspace_root=tmp_path)
        assert removed >= 1
        assert "bundles.demo.nodes.echo" not in sys.modules
        # Third-party packages survive — they were in `before` so the
        # `if name in before: continue` guard skipped them.
        assert "pydantic" in sys.modules

    def test_cleanup_returns_zero_when_no_bundle_imports(self, tmp_path: Path) -> None:
        before = snapshot_sys_modules()
        # Don't import anything from bundles/.
        removed = cleanup_bundle_imports(before=before, workspace_root=tmp_path)
        assert removed == 0

    def test_snapshot_is_immutable(self) -> None:
        snap = snapshot_sys_modules()
        # frozenset is the immutable-set type Python ships.
        assert isinstance(snap, frozenset)


# ---------------------------------------------------------------------------
# AC 0.7 — no executor logic in the shipped modules
# ---------------------------------------------------------------------------


class TestNoExecutorLogic:
    def test_run_module_does_not_walk_workflow_nodes(self) -> None:
        src = Path(__file__).resolve().parent.parent.parent / "src" / "workflows"
        for fname in ("run.py", "run_store.py", "module_isolation.py"):
            text = (src / fname).read_text(encoding="utf-8")
            assert "for node in workflow.nodes" not in text, fname
            assert "JINJA_ENV" not in text, fname
            assert "validate_workflow" not in text, fname


# ---------------------------------------------------------------------------
# Meta JSON shape — round-trip via serialised file
# ---------------------------------------------------------------------------


def test_meta_json_round_trips(tmp_path: Path) -> None:
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/hello", inputs={"x": 1})
    raw = json.loads((tmp_path / "runs" / str(run.run_id) / "meta.json").read_text())
    assert raw["run_id"] == run.run_id
    assert raw["workflow_ref"] == "demo/workflows/hello"
    assert raw["inputs"] == {"x": 1}
    assert raw["status"] == "pending"


_ = textwrap  # reserved for future fixture composition
