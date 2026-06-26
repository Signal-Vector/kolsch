"""The :class:`SessionStore` — public API per Module API Reference §3.

Composes the three pieces — :class:`EntityBackend`, the JSONL helpers,
and the content store — behind a single object the engine and CLI
consume. The class itself is a thin orchestrator; the protocol-shaped
sub-pieces let alternative implementations (Postgres backend in a
later phase) drop in by injecting a different backend.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from errors import SessionNotFound, ThreadNotFound
from schemas import (
    Actor,
    AgentId,
    ContentRef,
    Event,
    EventId,
    EventType,
    FromWorkflowRun,
    Session,
    SessionId,
    Thread,
    ThreadId,
)
from store.content import write_content_sync
from store.jsonl import (
    append_event_sync,
    jsonl_path,
    lock_path,
    read_first_event,
    read_last_event_for_thread,
    read_lines,
    recover_partial_final_line,
    safe_unlink,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from store.backend import EntityBackend


# ---------------------------------------------------------------------------
# Reconstruction result type
# ---------------------------------------------------------------------------


class ReconstructionResult(BaseModel):
    """Outcome of :meth:`SessionStore.reconstruct_from_filesystem`.

    Per spec §2.5: counts of sessions and threads found vs.
    reconstructed, plus warnings (e.g., files without
    ``session.opened``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sessions_found: int
    sessions_reconstructed: int
    threads_found: int
    threads_reconstructed: int
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# SessionStore
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _new_session_id() -> SessionId:
    return SessionId(f"ses_{uuid4()}")


def _new_thread_id() -> ThreadId:
    return ThreadId(f"th_{uuid4()}")


def _new_event_id() -> EventId:
    return EventId(f"ev_{uuid4()}")


class SessionStore:
    """Persistence layer for sessions, threads, events, and content.

    Constructor takes the platform root and a configured
    :class:`EntityBackend`. The store owns no global state; multiple
    instances over the same root + backend are safe because the
    durable state lives entirely on disk + in the backend.
    """

    def __init__(self, *, root: Path, backend: EntityBackend) -> None:
        self._root = root
        self._backend = backend

    # --- Session lifecycle (spec §2.1) ---------------------------------

    async def create_session(
        self,
        initial_agent_id: str,
        *,
        name: str | None = None,
        from_workflow_run: FromWorkflowRun | None = None,
    ) -> Session:
        """Create a session AND its default thread, then write `session.opened`.

        Spec §13.2's Event envelope requires ``thread_id``; for the
        first event in the file (``session.opened``) that id must
        point at a real thread. Phase 1 has one thread per session,
        so :meth:`create_session` allocates the default thread as
        part of session creation. Additional threads (Phase 2) are
        created explicitly via :meth:`create_thread` and remain a
        no-event operation.

        ``from_workflow_run`` is the Phase 3.2 parent pointer set by
        ``AgentRunner`` when an ``agent:`` workflow node spawns the
        session. When non-None it populates ``Session.from_workflow_run``
        AND injects a ``spawned_by`` entry into the ``session.opened``
        event's ``data`` payload per
        ``docs/explanation/workflow-event-log.md`` line 62. Chat- and
        API-spawned sessions leave it ``None`` and the payload is
        byte-identical to the pre-3.2 shape.
        """
        agent_id = AgentId(initial_agent_id)
        session_id = _new_session_id()
        thread_id = _new_thread_id()
        now = _now()

        session = Session(
            session_id=session_id,
            agent_id=agent_id,
            name=name or session_id,
            starred=False,
            archived=False,
            created_at=now,
            updated_at=now,
            from_workflow_run=from_workflow_run,
        )
        await self._backend.insert_session(session)

        default_thread = Thread(
            thread_id=thread_id,
            session_id=session_id,
            agent_id=agent_id,
            created_at=now,
        )
        await self._backend.insert_thread(default_thread)

        data: dict[str, object] = {
            "session_id": session_id,
            "initial_agent": {"id": agent_id, "name": agent_id},
        }
        if from_workflow_run is not None:
            data["spawned_by"] = {
                "from_workflow_run_id": str(from_workflow_run.workflow_run_id),
                "from_invocation_id": str(from_workflow_run.invocation_id),
                "from_workflow_event_id": str(from_workflow_run.event_id),
            }

        opener = Event(
            event_id=_new_event_id(),
            parent_event_id=None,
            thread_id=thread_id,
            session_id=session_id,
            type=EventType.SESSION_OPENED,
            data=data,
            actor=Actor(kind="system"),
            created_at=now,
        )
        await asyncio.to_thread(
            append_event_sync,
            root=self._root,
            session_id=session_id,
            thread_id=thread_id,
            event=opener,
        )
        return session

    async def get_session(self, session_id: SessionId) -> Session:
        result = await self._backend.get_session(session_id)
        if result is None:
            raise SessionNotFound(session_id=str(session_id))
        return result

    async def list_sessions(
        self,
        *,
        starred: bool | None = None,
        archived: bool | None = None,
        limit: int | None = None,
        order_by: Literal["created_at", "name"] = "created_at",
        order: Literal["asc", "desc"] = "desc",
    ) -> list[Session]:
        return await self._backend.list_sessions(
            starred=starred,
            archived=archived,
            limit=limit,
            order_by=order_by,
            order=order,
        )

    async def update_session(
        self,
        session_id: SessionId,
        *,
        name: str | None = None,
        starred: bool | None = None,
        archived: bool | None = None,
    ) -> Session:
        existing = await self.get_session(session_id)
        updated = existing.model_copy(
            update={
                "name": name if name is not None else existing.name,
                "starred": starred if starred is not None else existing.starred,
                "archived": (archived if archived is not None else existing.archived),
                "updated_at": _now(),
            }
        )
        await self._backend.update_session(updated)
        # The backend may re-stamp updated_at; refetch for the
        # authoritative record.
        refetched = await self._backend.get_session(session_id)
        return refetched if refetched is not None else updated

    async def delete_session(self, session_id: SessionId) -> None:
        await self._backend.delete_session(session_id)
        await asyncio.to_thread(safe_unlink, jsonl_path(self._root, session_id))
        await asyncio.to_thread(safe_unlink, lock_path(self._root, session_id))

    # --- Thread lifecycle (spec §2.2) ----------------------------------

    async def create_thread(
        self,
        session_id: SessionId,
        agent_id: AgentId,
    ) -> Thread:
        # Confirm the session exists; raises SessionNotFound otherwise.
        await self.get_session(session_id)
        thread = Thread(
            thread_id=_new_thread_id(),
            session_id=session_id,
            agent_id=agent_id,
            created_at=_now(),
        )
        await self._backend.insert_thread(thread)
        return thread

    async def get_thread(self, thread_id: ThreadId) -> Thread:
        result = await self._backend.get_thread(thread_id)
        if result is None:
            raise ThreadNotFound(thread_id=str(thread_id))
        return result

    async def list_threads(self, session_id: SessionId) -> list[Thread]:
        return await self._backend.list_threads(session_id)

    # --- Event operations (spec §2.3) ----------------------------------

    async def append_event(
        self,
        thread_id: ThreadId,
        event: Event,
    ) -> Event:
        # Resolve the thread → session mapping so we know which file
        # to append to. ThreadNotFound bubbles if absent.
        thread = await self.get_thread(thread_id)
        if event.session_id != thread.session_id:
            # Caller mistake — silently coercing would mask a real bug.
            # We trust the engine to set session_id consistently.
            msg = (
                f"event.session_id={event.session_id!r} does not match "
                f"thread.session_id={thread.session_id!r}"
            )
            raise ValueError(msg)
        return await asyncio.to_thread(
            append_event_sync,
            root=self._root,
            session_id=thread.session_id,
            thread_id=thread_id,
            event=event,
        )

    async def get_leaf(self, thread_id: ThreadId) -> Event | None:
        thread = await self.get_thread(thread_id)
        path = jsonl_path(self._root, thread.session_id)
        await asyncio.to_thread(recover_partial_final_line, path)
        return await asyncio.to_thread(
            read_last_event_for_thread,
            path,
            thread_id,
        )

    async def read_events(
        self,
        thread_id: ThreadId,
        *,
        from_event_id: str | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[Event]:
        thread = await self.get_thread(thread_id)
        path = jsonl_path(self._root, thread.session_id)
        await asyncio.to_thread(recover_partial_final_line, path)

        # Collect the lines on a worker thread so the iteration itself
        # is non-blocking even though disk I/O happens up front.
        all_events: list[Event] = await asyncio.to_thread(
            lambda: list(read_lines(path))
        )

        if from_event_id is not None:
            # Yield only events strictly after the sentinel.
            try:
                start = next(
                    i + 1
                    for i, e in enumerate(all_events)
                    if e.event_id == from_event_id
                )
            except StopIteration:
                start = len(all_events)
            all_events = all_events[start:]

        if limit is not None:
            all_events = all_events[:limit]

        for event in all_events:
            # Filter to the requested thread. Phase 1 has one thread
            # per session so this is identity, but the filter is the
            # forward-compatible behaviour for Phase 2 multi-thread.
            if event.thread_id == thread_id:
                yield event

    async def read_path_to_leaf(
        self,
        thread_id: ThreadId,
    ) -> AsyncIterator[Event]:
        # Phase 1: no branching → file order is the path. Phase 2's
        # branching-aware implementation replaces this body without
        # changing the call site.
        async for event in self.read_events(thread_id):
            yield event

    # --- Content store (spec §2.3) -------------------------------------

    async def write_content(self, content: bytes | str) -> ContentRef:
        payload = content if isinstance(content, bytes) else content.encode("utf-8")
        return await asyncio.to_thread(write_content_sync, self._root, payload)

    # --- Reconstruction (spec §2.5) ------------------------------------

    async def reconstruct_from_filesystem(self) -> ReconstructionResult:
        sessions_dir = self._root / "sessions"
        if not sessions_dir.exists():
            return ReconstructionResult(
                sessions_found=0,
                sessions_reconstructed=0,
                threads_found=0,
                threads_reconstructed=0,
            )

        warnings: list[str] = []
        sessions_found = 0
        sessions_reconstructed = 0
        threads_found = 0
        threads_reconstructed = 0
        seen_threads: set[ThreadId] = set()

        for path in sorted(sessions_dir.glob("*.jsonl")):
            sessions_found += 1
            try:
                session_id = SessionId(path.stem)
            except ValueError as exc:
                warnings.append(f"{path.name}: invalid session id ({exc}); skipped")
                sessions_found -= 1
                continue
            opener = await asyncio.to_thread(read_first_event, path)
            if opener is None or opener.get("type") != "session.opened":
                warnings.append(f"{session_id}: no session.opened event; skipped")
                sessions_found -= 1
                continue

            agent_blob = opener.get("data", {}).get("initial_agent", {})
            try:
                agent_id = AgentId(agent_blob.get("id", ""))
            except (ValueError, TypeError) as exc:
                warnings.append(
                    f"{session_id}: malformed initial_agent ({exc}); skipped"
                )
                sessions_found -= 1
                continue

            existing = await self._backend.get_session(session_id)
            if existing is None:
                now_ts = _parse_dt(opener.get("created_at")) or _now()
                rebuilt = Session(
                    session_id=session_id,
                    agent_id=agent_id,
                    # UI-state defaults per spec §2.5.
                    name=session_id,
                    starred=False,
                    archived=False,
                    created_at=now_ts,
                    updated_at=now_ts,
                )
                await self._backend.insert_session(rebuilt)
                sessions_reconstructed += 1

            # Scan the file's events for threads referenced. We use
            # the inferred (session, agent_id) since Phase 1 has no
            # explicit thread.created event.
            seen_in_file: set[ThreadId] = set()
            for event in await asyncio.to_thread(_safe_read_events, path):
                tid = event.thread_id
                if tid in seen_in_file:
                    continue
                seen_in_file.add(tid)
                if tid in seen_threads:
                    continue
                seen_threads.add(tid)
                threads_found += 1
                if await self._backend.get_thread(tid) is not None:
                    continue
                thread_record = Thread(
                    thread_id=tid,
                    session_id=session_id,
                    agent_id=agent_id,
                    created_at=_parse_dt(opener.get("created_at")) or _now(),
                )
                await self._backend.insert_thread(thread_record)
                threads_reconstructed += 1

        return ReconstructionResult(
            sessions_found=sessions_found,
            sessions_reconstructed=sessions_reconstructed,
            threads_found=threads_found,
            threads_reconstructed=threads_reconstructed,
            warnings=warnings,
        )


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _safe_read_events(path: Path) -> list[Event]:
    """Best-effort event read for reconstruct; corruption returns []."""
    try:
        return list(read_lines(path))
    except Exception:  # noqa: BLE001 — reconstruct is intentionally tolerant
        return []
