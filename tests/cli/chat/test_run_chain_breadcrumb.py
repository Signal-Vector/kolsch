"""spec-017 Test 5 — `PromptSource.run_chain` breadcrumb rendering.

Three passes:

1. Boot the `EventAdapter`, register a WorkflowsImpl with A→B→C
   nested chain, fire a workflow-source prompt on C. Verify:
   - `source.run_chain` has 3 entries, outermost-first: [A, B, C].
   - C's entry carries `node_name`; A and B have `""`.
2. The formatted breadcrumb string is
   `workflow A.ref → B.ref → C.ref · node <C.node>`.
3. Single-entry chain (top-level, no nesting) formats as
   `workflow <ref>` (no arrow, no trailing node clause) — spec-014
   compatibility fallback.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

import pytest

from cli.chat.event_adapter import EventAdapter
from cli.chat.services.workflows_impl import (
    WorkflowRunRecord,
    WorkflowsImpl,
)
from schemas.wire_kinds import WIRE_PROMPT_REQUESTED
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


class _FakeApp:
    """Bare-minimum ChatApp stand-in for EventAdapter tests.

    `mount_approval_block` accepts the same kwargs the real ChatApp
    ships with (spec-014's `parent_hint`). No transcript container is
    exposed so the adapter's card-mount path falls back to the
    in-memory stub.
    """

    def __init__(self) -> None:
        self._mount_calls: list[dict[str, Any]] = []

    async def mount_approval_block(
        self,
        rule_id: Any,
        action: Any,
        target: Any,
        *,
        parent_hint: Any = None,
    ) -> None:
        self._mount_calls.append({
            "rule_id": rule_id,
            "action": action,
            "target": target,
            "parent_hint": parent_hint,
        })

    def query_one(self, *_args: Any, **_kwargs: Any) -> Any:
        # Force _locate_transcript to fall through to the stub path.
        raise LookupError("no #transcript")


async def _mk_wfimpl(
    tmp_path: Path, store: SessionStore,
) -> WorkflowsImpl:
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


def _install_run(
    impl: WorkflowsImpl,
    *,
    ref: str,
    parent_run_id: WorkflowRunId | None = None,
) -> WorkflowRunId:
    """Synthetic registry population (see test_cascade_cancel helper)."""
    run_id = WorkflowRunId(f"wrk_{uuid4()}")
    record = WorkflowRunRecord(
        run_id=run_id,
        ref=ref,
        phase="running",
        card=None,
        created_at=datetime.now(tz=UTC),
        parent_run_id=parent_run_id,
    )
    impl._workflow_registry[run_id] = record
    return run_id


async def test_run_chain_populated_and_formatted_for_nested_chain(
    tmp_path: Path, store: SessionStore,
) -> None:
    """A→B→C nested; prompt on C. run_chain outermost-first + formatted."""
    impl = await _mk_wfimpl(tmp_path, store)
    try:
        a = _install_run(impl, ref="A.ref")
        b = _install_run(impl, ref="B.ref", parent_run_id=a)
        c = _install_run(impl, ref="C.ref", parent_run_id=b)

        app = _FakeApp()
        adapter = EventAdapter(app, workflows_impl=impl)  # type: ignore[arg-type]

        payload: dict[str, Any] = {
            "prompt_id": "prm_test1",
            "rule_id": "rl_x",
            "action": "kolsch/tools/bash",
            "target": "ls",
            "source": {
                "kind": "workflow",
                "id": str(c),
                "run_id": str(c),
                "node_name": "build",
                "run_chain": [],
            },
        }
        await adapter._handle_prompt_requested(payload)

        # AC #10: run_chain populated, outermost-first.
        chain = payload["source"]["run_chain"]
        assert isinstance(chain, list)
        assert len(chain) == 3
        assert chain[0]["run_id"] == str(a)
        assert chain[0]["ref"] == "A.ref"
        assert chain[0]["node_name"] == ""
        assert chain[1]["run_id"] == str(b)
        assert chain[1]["ref"] == "B.ref"
        assert chain[1]["node_name"] == ""
        assert chain[2]["run_id"] == str(c)
        assert chain[2]["ref"] == "C.ref"
        assert chain[2]["node_name"] == "build"

        # AC #11: parent_hint breadcrumb passed to mount_approval_block.
        assert len(app._mount_calls) == 1
        parent_hint = app._mount_calls[0]["parent_hint"]
        assert parent_hint == "workflow A.ref → B.ref → C.ref · node build"
    finally:
        await impl.aclose()


async def test_single_workflow_breadcrumb_no_arrow(
    tmp_path: Path, store: SessionStore,
) -> None:
    """Single-entry chain formats as `workflow <ref>` (spec-014 compat)."""
    impl = await _mk_wfimpl(tmp_path, store)
    try:
        top = _install_run(impl, ref="only.ref")

        app = _FakeApp()
        adapter = EventAdapter(app, workflows_impl=impl)  # type: ignore[arg-type]

        payload: dict[str, Any] = {
            "prompt_id": "prm_top",
            "rule_id": None,
            "action": "kolsch/tools/bash",
            "target": "pwd",
            "source": {
                "kind": "workflow",
                "id": str(top),
                "run_id": str(top),
                "node_name": "lint",
                "run_chain": [],
            },
        }
        await adapter._handle_prompt_requested(payload)

        chain = payload["source"]["run_chain"]
        assert len(chain) == 1
        assert chain[0]["run_id"] == str(top)
        assert chain[0]["ref"] == "only.ref"

        parent_hint = app._mount_calls[0]["parent_hint"]
        # Single-workflow default keeps the spec-014 shape:
        # `workflow <ref>` with the innermost node_name appended.
        assert parent_hint == "workflow only.ref · node lint"
    finally:
        await impl.aclose()


async def test_run_chain_helper_shape() -> None:
    """`_build_run_chain` + `_format_run_chain` direct smoke.

    Belt-and-braces so a regression in either helper surfaces
    without booting the adapter's full prompt-request path.
    """
    tmp_path = Path("/tmp") / f"kolsch_run_chain_helper_{uuid4()}"
    tmp_path.mkdir(parents=True, exist_ok=True)
    backend = SqliteBackend(tmp_path / ".platform" / "entities.db")
    (tmp_path / ".platform").mkdir(exist_ok=True)
    await backend.initialize()
    try:
        store = SessionStore(root=tmp_path, backend=backend)
        impl = await _mk_wfimpl(tmp_path, store)
        try:
            a = _install_run(impl, ref="A.ref")
            b = _install_run(impl, ref="B.ref", parent_run_id=a)

            adapter = EventAdapter(
                _FakeApp(),  # type: ignore[arg-type]
                workflows_impl=impl,
            )
            chain = adapter._build_run_chain(str(b), "leaf")
            # Two entries, outermost-first.
            assert [e["ref"] for e in chain] == ["A.ref", "B.ref"]
            assert chain[0]["node_name"] == ""
            assert chain[1]["node_name"] == "leaf"

            formatted = adapter._format_run_chain(chain)
            assert formatted == "workflow A.ref → B.ref · node leaf"

            # Empty chain → empty string.
            assert adapter._format_run_chain([]) == ""
        finally:
            await impl.aclose()
    finally:
        await backend.close()
