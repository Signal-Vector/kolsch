"""Component tests for Phase 3.2 Step 0 — session-side workflow parent pointer.

Covers ``Session.from_workflow_run`` round-trip + the ``spawned_by`` injection
into the ``session.opened`` event payload per ``docs/explanation/workflow-event-log.md``
line 62. The companion ``AgentRunner`` (Steps 1-3) consumes this surface; here
we exercise it via direct ``SessionStore.create_session`` calls so the data-model
+ store plumbing is verifiable in isolation from any runner wiring.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from schemas import (
    EventId,
    FromWorkflowRun,
)
from store import EntityBackend, SessionStore, SqliteBackend
from workflows.ids import InvocationId, WorkflowRunId

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.fixture
async def backend(tmp_path: Path) -> AsyncIterator[EntityBackend]:
    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    b = SqliteBackend(platform_dir / "entities.db")
    await b.initialize()
    yield b
    await b.close()


@pytest.fixture
async def store(tmp_path: Path, backend: EntityBackend) -> SessionStore:
    return SessionStore(root=tmp_path, backend=backend)


def _read_first_event(root: Path, session_id: str) -> dict[str, object]:
    jsonl = root / "sessions" / f"{session_id}.jsonl"
    with jsonl.open() as f:
        return json.loads(f.readline())


def _new_pointer() -> FromWorkflowRun:
    return FromWorkflowRun(
        workflow_run_id=WorkflowRunId(f"wrk_{uuid4()}"),
        invocation_id=InvocationId(f"inv_{uuid4()}"),
        event_id=EventId(f"ev_{uuid4()}"),
    )


# ---------------------------------------------------------------------------
# Backward-compat: chat-spawned sessions are byte-identical to pre-3.2
# ---------------------------------------------------------------------------


async def test_create_session_without_pointer_omits_spawned_by(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    session = await store.create_session("ag_test", name="chat-session")

    assert session.from_workflow_run is None

    first = _read_first_event(tmp_path, session.session_id)
    assert first["type"] == "session.opened"
    data = first["data"]
    assert isinstance(data, dict)
    # The session.opened payload must be byte-identical to the pre-3.2 shape:
    # only `session_id` + `initial_agent`. No `spawned_by` key.
    assert set(data.keys()) == {"session_id", "initial_agent"}
    assert "spawned_by" not in data


async def test_create_session_without_pointer_round_trips_none(
    store: SessionStore,
) -> None:
    session = await store.create_session("ag_test")
    rebuilt = await store.get_session(session.session_id)
    assert rebuilt.from_workflow_run is None


# ---------------------------------------------------------------------------
# Phase 3.2: pointer populates the session field + spawned_by payload
# ---------------------------------------------------------------------------


async def test_create_session_with_pointer_populates_field(
    store: SessionStore,
) -> None:
    fwr = _new_pointer()
    session = await store.create_session(
        "ag_test",
        name="workflow-spawned",
        from_workflow_run=fwr,
    )
    assert session.from_workflow_run == fwr


async def test_create_session_with_pointer_round_trips_through_get(
    store: SessionStore,
) -> None:
    fwr = _new_pointer()
    created = await store.create_session(
        "ag_test",
        name="workflow-spawned",
        from_workflow_run=fwr,
    )
    rebuilt = await store.get_session(created.session_id)
    assert rebuilt.from_workflow_run == fwr


async def test_create_session_with_pointer_injects_spawned_by_payload(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    fwr = _new_pointer()
    session = await store.create_session(
        "ag_test",
        name="workflow-spawned",
        from_workflow_run=fwr,
    )

    first = _read_first_event(tmp_path, session.session_id)
    assert first["type"] == "session.opened"
    data = first["data"]
    assert isinstance(data, dict)

    # `session_id` + `initial_agent` survive; `spawned_by` is added with the
    # canonical key names documented in workflow-event-log.md line 62.
    assert data["session_id"] == session.session_id
    assert data["initial_agent"] == {"id": "ag_test", "name": "ag_test"}

    spawned_by = data["spawned_by"]
    assert spawned_by == {
        "from_workflow_run_id": str(fwr.workflow_run_id),
        "from_invocation_id": str(fwr.invocation_id),
        "from_workflow_event_id": str(fwr.event_id),
    }
    # IDs must be str-coerced (not raw PrefixedId instances) so the JSONL
    # serializer emits plain strings on disk.
    for key in (
        "from_workflow_run_id",
        "from_invocation_id",
        "from_workflow_event_id",
    ):
        assert isinstance(spawned_by[key], str)
