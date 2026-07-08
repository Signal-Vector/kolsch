"""spec-012 Test 3 — `status` cold-read via meta.json + JSONL tail.

Three passes:

1. Subprocess-run analog: `meta.json` with `status="running"`,
   in-memory registry empty. `status()` returns `phase="running"` via
   cold-read.
2. Same setup but JSONL tail carries a `RUN_COMPLETED` event — the
   tail beats the meta status; result is `phase="success"`.
3. Run absent from BOTH registry AND store returns `phase="unknown"`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

import pytest

from cli.chat.services.workflows_impl import WorkflowsImpl
from store import SessionStore, SqliteBackend
from workflows.ids import WorkflowRunId
from workflows.run_store import WorkflowRunStore


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    try:
        yield SessionStore(root=tmp_path, backend=backend)
    finally:
        await backend.close()


async def _mk_impl(tmp_path: Path, store: SessionStore) -> WorkflowsImpl:
    session = await store.create_session("ag_test", name="w")
    threads = await store.list_threads(session.session_id)
    run_store = WorkflowRunStore(tmp_path)
    return WorkflowsImpl(
        runtime=None,
        cards=None,
        prompts=None,
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        run_store=run_store,
    )


def _mint_meta(
    runs_root: Path, run_id: WorkflowRunId, status: str,
) -> Path:
    """Write a minimal but validating `meta.json` under runs/<run_id>/."""
    run_dir = runs_root / str(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "run_id": str(run_id),
        "trace_id": f"tr_{uuid4()}",
        "workflow_ref": "kolsch/workflows/test",
        "status": status,
        "inputs": {},
        "created_at": "2026-07-05T00:00:00+00:00",
    }
    meta_path = run_dir / "meta.json"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    return meta_path


async def test_coldread_running_meta_no_registry(
    tmp_path: Path, store: SessionStore,
) -> None:
    impl = await _mk_impl(tmp_path, store)
    try:
        rid = WorkflowRunId(f"wrk_{uuid4()}")
        _mint_meta(tmp_path / "runs", rid, "running")
        # No registry entry — cold-read path.
        assert rid not in impl._workflow_registry
        result = impl.status(rid)
        assert result.phase == "running"
    finally:
        await impl.aclose()


async def test_coldread_terminal_in_jsonl_beats_meta(
    tmp_path: Path, store: SessionStore,
) -> None:
    impl = await _mk_impl(tmp_path, store)
    try:
        rid = WorkflowRunId(f"wrk_{uuid4()}")
        _mint_meta(tmp_path / "runs", rid, "running")
        # Append a terminal event to the JSONL.
        events_path = tmp_path / "runs" / str(rid) / "events.jsonl"
        events_path.write_text(
            json.dumps(
                {
                    "event_id": f"ev_{uuid4()}",
                    "kind": "workflow.run.completed",
                    "workflow_run_id": str(rid),
                    "trace_id": f"tr_{uuid4()}",
                    "ts": "2026-07-05T00:00:01+00:00",
                    "payload": {},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result = impl.status(rid)
        assert result.phase == "success"
    finally:
        await impl.aclose()


async def test_coldread_missing_returns_unknown(
    tmp_path: Path, store: SessionStore,
) -> None:
    impl = await _mk_impl(tmp_path, store)
    try:
        rid = WorkflowRunId(f"wrk_{uuid4()}")
        # No registry, no meta.
        result = impl.status(rid)
        assert result.phase == "unknown"
    finally:
        await impl.aclose()
