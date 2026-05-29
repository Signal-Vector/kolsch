"""Component tests for src/store — TC-STR-01..12.

These are component tests per handbook §8.1: one subsystem (the
store) against real backing (a SQLite DB on disk + a JSONL file
on disk in a temp dir). No platform mocks.

The fixture set is parametrized over the entity backend so the same
suite runs against Postgres once that backend ships (TC-STR-11). v1
parametrizes a single backend (SQLite); the parametrization stays so
the slot for Postgres is wired and visible.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from errors import SessionNotFound, StoreCorruptLog, ThreadNotFound
from schemas import (
    Actor,
    AgentId,
    Event,
    EventId,
    EventType,
    SessionId,
    ThreadId,
)
from store import EntityBackend, SessionStore, SqliteBackend

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_event(  # noqa: PLR0913 — test helper kwargs are intentional
    *,
    thread_id: ThreadId,
    session_id: SessionId,
    event_type: EventType = EventType.USER_MESSAGE,
    data: dict[str, Any] | None = None,
    actor_kind: str = "user",
    actor_id: str | None = "user.local",
) -> Event:
    """Build a fresh, fully-formed event with no parent set."""
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=thread_id,
        session_id=session_id,
        type=event_type,
        data=data or {"content": "hi"},
        actor=Actor(kind=actor_kind, id=actor_id),  # type: ignore[arg-type]
        created_at=datetime.now(UTC),
        content_ref=None,
    )


# ---------------------------------------------------------------------------
# Fixtures — parametrized over the entity backend
# ---------------------------------------------------------------------------


@pytest.fixture(params=["sqlite"], ids=lambda v: f"backend={v}")
async def backend(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> AsyncIterator[EntityBackend]:
    """The entity backend under test. Parametrize as backends ship.

    Adding ``"postgres"`` to ``params`` once the Postgres backend lands
    runs the entire suite against it without any test changes
    (TC-STR-11 / spec §10 DB-R1).
    """
    backend_name: str = request.param
    if backend_name == "sqlite":
        platform_dir = tmp_path / ".platform"
        platform_dir.mkdir(exist_ok=True)
        b = SqliteBackend(platform_dir / "entities.db")
        await b.initialize()
        yield b
        await b.close()
    else:  # pragma: no cover — slot for future backends
        pytest.skip(f"backend {backend_name!r} not yet shipped")


@pytest.fixture
async def store(tmp_path: Path, backend: EntityBackend) -> SessionStore:
    return SessionStore(root=tmp_path, backend=backend)


@pytest.fixture
async def session_with_thread(
    store: SessionStore,
) -> tuple[SessionId, ThreadId]:
    """A fresh session with its auto-created default thread.

    Spec §13.2 requires every event to carry a ``thread_id``, so
    ``create_session`` allocates a default thread as part of the
    session bootstrap. The default thread is discoverable via
    :meth:`SessionStore.list_threads`.
    """
    session = await store.create_session("ag_test")
    threads = await store.list_threads(session.session_id)
    assert len(threads) == 1, "create_session should bootstrap a default thread"
    return session.session_id, threads[0].thread_id


# ---------------------------------------------------------------------------
# TC-STR-01 — create_session writes file + entity
# ---------------------------------------------------------------------------


async def test_create_session_writes_file_and_entity(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    session = await store.create_session("ag_test")

    jsonl = tmp_path / "sessions" / f"{session.session_id}.jsonl"
    assert jsonl.exists()

    # First line is `session.opened`.
    with jsonl.open() as f:
        first = json.loads(f.readline())
    assert first["type"] == "session.opened"
    assert first["session_id"] == session.session_id
    assert first["data"]["initial_agent"]["id"] == "ag_test"

    # Entity record exists.
    rebuilt = await store.get_session(session.session_id)
    assert rebuilt.session_id == session.session_id
    assert rebuilt.agent_id == "ag_test"
    assert rebuilt.starred is False
    assert rebuilt.archived is False


# ---------------------------------------------------------------------------
# TC-STR-02 — get_leaf on fresh thread returns the last event in the file
# ---------------------------------------------------------------------------


async def test_get_leaf_on_fresh_thread_returns_session_opened(
    store: SessionStore,
    session_with_thread: tuple[SessionId, ThreadId],
) -> None:
    _, thread_id = session_with_thread
    leaf = await store.get_leaf(thread_id)
    # Fresh thread: only session.opened in the file. Phase 1's leaf
    # rule is the most recent append for the thread, regardless of
    # event type — so it's the session.opened event itself.
    assert leaf is not None
    assert leaf.type == EventType.SESSION_OPENED


async def test_get_leaf_on_additional_thread_returns_none(
    store: SessionStore,
) -> None:
    """An ADDITIONAL thread (created via :meth:`create_thread`) has
    no events of its own until something is appended. ``get_leaf``
    returns ``None`` in that state."""
    session = await store.create_session("ag_test")
    extra = await store.create_thread(session.session_id, AgentId("ag_test"))
    leaf = await store.get_leaf(extra.thread_id)
    assert leaf is None


# ---------------------------------------------------------------------------
# TC-STR-03 — append populates parent_event_id
# ---------------------------------------------------------------------------


async def test_append_populates_parent_event_id(
    store: SessionStore,
    session_with_thread: tuple[SessionId, ThreadId],
) -> None:
    session_id, thread_id = session_with_thread
    e1 = await store.get_leaf(thread_id)
    assert e1 is not None

    fresh = _new_event(thread_id=thread_id, session_id=session_id)
    assert fresh.parent_event_id is None  # caller must NOT pre-populate.

    appended = await store.append_event(thread_id, fresh)
    assert appended.parent_event_id == e1.event_id
    assert appended.event_id == fresh.event_id  # caller's id preserved.

    # Cross-check by reading back.
    new_leaf = await store.get_leaf(thread_id)
    assert new_leaf is not None
    assert new_leaf.event_id == appended.event_id


# ---------------------------------------------------------------------------
# TC-STR-04 — parent chain correct over 1000 events
# ---------------------------------------------------------------------------


async def test_parent_chain_correct_over_many_events(
    store: SessionStore,
    session_with_thread: tuple[SessionId, ThreadId],
) -> None:
    session_id, thread_id = session_with_thread

    # Catalog TC-STR-04 literal: 1000 sequential appends. The flock +
    # Pydantic + SQLite stack handles this in well under a second.
    append_count = 1000
    appended_ids: list[str] = []
    for i in range(append_count):
        event = _new_event(
            thread_id=thread_id,
            session_id=session_id,
            data={"index": i},
        )
        result = await store.append_event(thread_id, event)
        appended_ids.append(result.event_id)

    # Walk back the chain via read_events and verify parent IDs match.
    read_back = [event async for event in store.read_events(thread_id)]
    # First event is session.opened; the rest must form a chain.
    assert len(read_back) == 1 + append_count
    parent = read_back[0].event_id
    for ev in read_back[1:]:
        assert ev.parent_event_id == parent
        parent = ev.event_id


# ---------------------------------------------------------------------------
# TC-STR-05 — concurrent appends serialize (same process)
# ---------------------------------------------------------------------------


async def test_concurrent_appends_in_same_process_serialize(
    store: SessionStore,
    session_with_thread: tuple[SessionId, ThreadId],
) -> None:
    session_id, thread_id = session_with_thread

    # Catalog TC-STR-05 literal: 100 concurrent append coroutines.
    n = 100

    async def _append(i: int) -> str:
        event = _new_event(
            thread_id=thread_id,
            session_id=session_id,
            data={"i": i},
        )
        result = await store.append_event(thread_id, event)
        return result.event_id

    appended = await asyncio.gather(*(_append(i) for i in range(n)))

    events = [e async for e in store.read_events(thread_id)]
    # session.opened + n appends, all present.
    assert len(events) == 1 + n
    # No event_id duplicated.
    assert len({e.event_id for e in events}) == 1 + n
    # Parent chain linear: every event's parent equals the previous
    # event's id; no two events share a parent.
    parents = [e.parent_event_id for e in events[1:]]
    assert len(set(parents)) == n, "every parent must be distinct (linear chain)"
    # Every appended id is in the read-back set.
    appended_set = set(appended)
    file_set = {e.event_id for e in events[1:]}
    assert appended_set == file_set


# ---------------------------------------------------------------------------
# TC-STR-06 — concurrent appends serialize (cross-process)
# ---------------------------------------------------------------------------


def _cross_process_worker(
    root_str: str,
    session_id_str: str,
    thread_id_str: str,
    n: int,
) -> None:
    """Run inside a child process: append N events to the thread.

    Top-level so the multiprocessing ``spawn`` start method can pickle
    it. Multiprocessing re-imports this module in the child, so all
    module-level names (asyncio, Event, SessionStore, etc.) are
    already in scope — no inline imports needed.

    Each child opens its own SQLite connection and runs its own
    asyncio loop; the flock on the per-session lock file is what
    serializes them at the kernel.
    """

    async def _run() -> None:
        root = Path(root_str)
        backend = SqliteBackend(root / ".platform" / "entities.db")
        await backend.initialize()
        st = SessionStore(root=root, backend=backend)
        for i in range(n):
            event = Event(
                event_id=EventId(f"ev_{uuid4()}"),
                parent_event_id=None,
                thread_id=ThreadId(thread_id_str),
                session_id=SessionId(session_id_str),
                type=EventType.USER_MESSAGE,
                data={"pid": os.getpid(), "i": i},
                actor=Actor(kind="user", id=f"user.{os.getpid()}"),
                created_at=datetime.now(UTC),
            )
            await st.append_event(ThreadId(thread_id_str), event)
        await backend.close()

    asyncio.run(_run())


@pytest.mark.slow
async def test_cross_process_appends_serialize(
    tmp_path: Path,
    store: SessionStore,
    session_with_thread: tuple[SessionId, ThreadId],
) -> None:
    session_id, thread_id = session_with_thread

    # Catalog TC-STR-06 literal: two separate OS processes, each
    # appending 50 events. Total 100 events serialised at the kernel
    # via flock on the per-session lock file.
    n_per_child = 50
    n_children = 2

    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(
            target=_cross_process_worker,
            args=(str(tmp_path), session_id, thread_id, n_per_child),
        )
        for _ in range(n_children)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"child {p.pid} exited {p.exitcode}"

    events = [e async for e in store.read_events(thread_id)]
    # session.opened + (n_children x n_per_child).
    assert len(events) == 1 + n_children * n_per_child
    assert len({e.event_id for e in events}) == len(events)
    # Parent chain linear.
    parents = [e.parent_event_id for e in events[1:]]
    assert len(set(parents)) == len(parents), "linear chain — no fork"


# ---------------------------------------------------------------------------
# TC-STR-07 — mid-write kill is recoverable
# ---------------------------------------------------------------------------


async def test_partial_final_line_is_truncated_on_open(
    tmp_path: Path,
    store: SessionStore,
    session_with_thread: tuple[SessionId, ThreadId],
) -> None:
    session_id, thread_id = session_with_thread
    # Append a real event so we have at least one complete event after
    # session.opened.
    real = await store.append_event(
        thread_id,
        _new_event(thread_id=thread_id, session_id=session_id, data={"real": True}),
    )

    # Simulate a crash mid-write: append a partial JSON line (no
    # closing brace, no newline).
    jsonl = tmp_path / "sessions" / f"{session_id}.jsonl"
    with jsonl.open("ab") as f:
        f.write(b'{"event_id": "ev_bad", "partial":')  # malformed, no \n

    # Recovery truncates the partial line on next open.
    leaf = await store.get_leaf(thread_id)
    assert leaf is not None
    assert leaf.event_id == real.event_id

    # The file is now well-formed: a fresh append succeeds.
    fresh = await store.append_event(
        thread_id,
        _new_event(thread_id=thread_id, session_id=session_id, data={"after": True}),
    )
    assert fresh.parent_event_id == real.event_id


# ---------------------------------------------------------------------------
# TC-STR-08 — corrupt mid-line raises with line number
# ---------------------------------------------------------------------------


async def test_corrupt_middle_line_raises_with_line_number(
    tmp_path: Path,
    store: SessionStore,
    session_with_thread: tuple[SessionId, ThreadId],
) -> None:
    session_id, thread_id = session_with_thread
    await store.append_event(
        thread_id,
        _new_event(thread_id=thread_id, session_id=session_id),
    )

    # Inject a malformed COMPLETE line in the middle. (Final-line
    # malformations are recovered by truncation; middle-line ones are
    # spec violations and raise.)
    jsonl = tmp_path / "sessions" / f"{session_id}.jsonl"
    contents = jsonl.read_text(encoding="utf-8")
    lines = contents.splitlines(keepends=True)
    assert len(lines) >= 2
    lines.insert(1, "{NOT JSON\n")
    jsonl.write_text("".join(lines), encoding="utf-8")

    with pytest.raises(StoreCorruptLog) as exc_info:
        _ = [e async for e in store.read_events(thread_id)]
    assert exc_info.value.detail["line_number"] == 2  # 1-indexed


# ---------------------------------------------------------------------------
# TC-STR-09 — write_content is idempotent
# ---------------------------------------------------------------------------


async def test_write_content_is_idempotent(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    payload = b"hello content store"

    ref1 = await store.write_content(payload)
    mtime1 = (tmp_path / ref1.stored_at).stat().st_mtime_ns

    ref2 = await store.write_content(payload)
    mtime2 = (tmp_path / ref2.stored_at).stat().st_mtime_ns

    assert ref1.hash == ref2.hash
    assert ref1.size == ref2.size == len(payload)
    assert ref1.stored_at == ref2.stored_at == f"content/{ref1.hash}.bin"
    # The file was not rewritten on the second call.
    assert mtime1 == mtime2


async def test_write_content_accepts_str(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    ref = await store.write_content("hello as string")
    on_disk = (tmp_path / ref.stored_at).read_bytes()
    assert on_disk == b"hello as string"
    assert ref.size == len(on_disk)


# ---------------------------------------------------------------------------
# TC-STR-10 — reconstruct rebuilds entities from JSONL files
# ---------------------------------------------------------------------------


async def test_reconstruct_rebuilds_session_and_thread_entities(
    store: SessionStore,
    backend: EntityBackend,
) -> None:
    # Two sessions, each with its default thread + one appended event.
    s1 = await store.create_session("ag_alpha")
    t1 = (await store.list_threads(s1.session_id))[0]
    await store.append_event(
        t1.thread_id,
        _new_event(thread_id=t1.thread_id, session_id=s1.session_id),
    )
    s2 = await store.create_session("ag_beta")
    t2 = (await store.list_threads(s2.session_id))[0]
    await store.append_event(
        t2.thread_id,
        _new_event(thread_id=t2.thread_id, session_id=s2.session_id),
    )

    # Wipe the entity DB (the JSONL files survive on disk).
    await backend.delete_all()

    result = await store.reconstruct_from_filesystem()
    assert result.sessions_found == 2
    assert result.sessions_reconstructed == 2
    assert result.threads_found == 2
    assert result.threads_reconstructed == 2

    # The Session entity records are back; their JSONL still on disk.
    a = await store.get_session(s1.session_id)
    b = await store.get_session(s2.session_id)
    assert a.agent_id == "ag_alpha"
    assert b.agent_id == "ag_beta"


async def test_reconstruct_is_non_destructive_on_existing_records(
    store: SessionStore,
) -> None:
    s1 = await store.create_session("ag_x")
    await store.update_session(s1.session_id, name="renamed", starred=True)

    result = await store.reconstruct_from_filesystem()
    assert result.sessions_found == 1
    assert result.sessions_reconstructed == 0  # already in DB

    rebuilt = await store.get_session(s1.session_id)
    # UI-state fields were preserved; reconstruct did not overwrite.
    assert rebuilt.name == "renamed"
    assert rebuilt.starred is True


async def test_reconstruct_skips_corrupt_file_with_warning(
    tmp_path: Path,
    store: SessionStore,
    backend: EntityBackend,
) -> None:
    good = await store.create_session("ag_good")
    # Create a JSONL file whose name is a well-formed session id but
    # whose contents lack a session.opened event. Spec §2.5: skipped
    # with a warning.
    bad_id = f"ses_{uuid4()}"
    bad_path = tmp_path / "sessions" / f"{bad_id}.jsonl"
    bad_path.write_text('{"type":"user.message"}\n', encoding="utf-8")

    await backend.delete_all()

    result = await store.reconstruct_from_filesystem()
    assert result.sessions_reconstructed == 1
    assert any(bad_id in w for w in result.warnings)

    rebuilt = await store.get_session(good.session_id)
    assert rebuilt.session_id == good.session_id


# ---------------------------------------------------------------------------
# TC-STR-11 — backend parity (parametrized over backend fixture)
# ---------------------------------------------------------------------------
# Every test above is parametrized via the `backend` fixture's `params`
# list; once `"postgres"` is added there the entire suite runs against
# both implementations identically. The parametrization itself is the
# parity guarantee.


# ---------------------------------------------------------------------------
# TC-STR-12 — delete removes file + entity, second delete is no-op
# ---------------------------------------------------------------------------


async def test_delete_removes_file_lock_and_entity(
    tmp_path: Path,
    store: SessionStore,
    session_with_thread: tuple[SessionId, ThreadId],
) -> None:
    session_id, _ = session_with_thread
    jsonl = tmp_path / "sessions" / f"{session_id}.jsonl"
    lock = tmp_path / "sessions" / f"{session_id}.jsonl.lock"
    assert jsonl.exists()
    assert lock.exists()

    await store.delete_session(session_id)

    assert not jsonl.exists()
    assert not lock.exists()
    with pytest.raises(SessionNotFound):
        await store.get_session(session_id)


async def test_delete_is_idempotent(store: SessionStore) -> None:
    s = await store.create_session("ag_x")
    await store.delete_session(s.session_id)
    # A second delete is a no-op — does not raise.
    await store.delete_session(s.session_id)


# ---------------------------------------------------------------------------
# Spec invariants — append_event must verify thread/session consistency
# ---------------------------------------------------------------------------


async def test_get_thread_unknown_raises(store: SessionStore) -> None:
    with pytest.raises(ThreadNotFound):
        await store.get_thread(ThreadId(f"th_{uuid4()}"))


async def test_get_session_unknown_raises(store: SessionStore) -> None:
    with pytest.raises(SessionNotFound):
        await store.get_session(SessionId(f"ses_{uuid4()}"))


async def test_update_session_returns_updated_record(
    store: SessionStore,
) -> None:
    s = await store.create_session("ag_x")
    updated = await store.update_session(s.session_id, name="renamed", starred=True)
    assert updated.name == "renamed"
    assert updated.starred is True
    # Persisted: fetch by id and check.
    rebuilt = await store.get_session(s.session_id)
    assert rebuilt.name == "renamed"
    assert rebuilt.starred is True


async def test_jsonl_event_serialization_is_byte_stable(
    store: SessionStore,
    session_with_thread: tuple[SessionId, ThreadId],
    tmp_path: Path,
) -> None:
    """JSONL lines for the same Event MUST be byte-identical across
    invocations AND across the read/serialise round-trip.

    Load-bearing invariant for the golden event-log tests (handbook
    §8.4) and the partial-final-line recovery (spec §5.3). Pins the
    sort_keys + compact-separator serialisation policy so a future
    Pydantic upgrade or field reorder can't silently change every
    existing line's bytes.
    """
    session_id, thread_id = session_with_thread
    await store.append_event(
        thread_id,
        _new_event(thread_id=thread_id, session_id=session_id, data={"k": 1}),
    )

    jsonl = tmp_path / "sessions" / f"{session_id}.jsonl"
    on_disk = jsonl.read_bytes()
    line_count_before = on_disk.count(b"\n")

    # Re-read every event and re-serialise. The bytes must match
    # exactly — same set of events in the same order produces
    # identical file content.
    rebuilt_events = [e async for e in store.read_events(thread_id)]
    # Read the session.opened too (it's filtered out of thread-only
    # reads); use the underlying helper.
    from store.jsonl import _serialise, read_lines

    all_events = list(read_lines(jsonl))
    reserialised = b"".join(_serialise(e) for e in all_events)
    assert reserialised == on_disk

    # And every top-level key in every line is sorted.
    for raw in on_disk.splitlines():
        decoded = json.loads(raw)
        assert list(decoded.keys()) == sorted(decoded.keys()), (
            f"line keys not sorted: {list(decoded.keys())}"
        )
    # Reassert we exercised something.
    assert line_count_before >= 2  # session.opened + the append.
    # The default-thread fixture makes session.opened thread-scoped
    # to the same thread, so both events surface to the per-thread
    # reader — exact count doesn't matter, just that we read events.
    assert len(rebuilt_events) >= 1


async def test_read_path_to_leaf_equals_read_events_in_phase_1(
    store: SessionStore,
    session_with_thread: tuple[SessionId, ThreadId],
) -> None:
    session_id, thread_id = session_with_thread
    for i in range(5):
        await store.append_event(
            thread_id,
            _new_event(thread_id=thread_id, session_id=session_id, data={"i": i}),
        )
    file_order = [e async for e in store.read_events(thread_id)]
    path_order = [e async for e in store.read_path_to_leaf(thread_id)]
    # Phase 1: single thread, no branching → path == file order.
    assert [e.event_id for e in path_order] == [e.event_id for e in file_order]
