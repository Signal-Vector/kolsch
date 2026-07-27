"""The `SessionStore` — public persistence surface for the engine and CLI.

Composes the three pieces — [`EntityBackend`][store.backend.EntityBackend],
the JSONL helpers, and the content sidecar store — behind one object
the engine and CLI consume. The class itself is a thin orchestrator;
the protocol-shaped sub-pieces let alternative implementations
(a Postgres backend in a later phase) drop in by injecting a different
backend.

See the
[session/run store contract](../explanation/session-run-store-contract.md)
for the full set of invariants this layer upholds.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
    SessionMeta,
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

    from store.backend import EntityBackend


# ---------------------------------------------------------------------------
# Reconstruction result type
# ---------------------------------------------------------------------------


class _Sentinel:
    """Sentinel type for `update_meta` — distinguishes "unchanged" from `None`.

    `None` is a valid value for `event_adapter_bookmark` (clears the
    bookmark). `_SENTINEL` means "leave the field unchanged". Standard
    Python idiom — a private class + a private singleton.
    """


_SENTINEL = _Sentinel()


class ReconstructionResult(BaseModel):
    """Outcome of filesystem-driven session reconstruction.

    Returned by
    [`SessionStore.reconstruct_from_filesystem`][store.session_store.SessionStore.reconstruct_from_filesystem].
    Counts sessions and threads found versus reconstructed, plus
    warnings (for example, JSONL files missing a `session.opened`
    event).
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
    [`EntityBackend`][store.backend.EntityBackend]. The store owns no
    global state; multiple instances over the same root and backend
    are safe because the durable state lives entirely on disk and in
    the backend.
    """

    def __init__(self, *, root: Path, backend: EntityBackend) -> None:
        self._root = root
        self._backend = backend

    # --- Session lifecycle ---------------------------------------------

    async def create_session(
        self,
        initial_agent_id: str,
        *,
        name: str | None = None,
        from_workflow_run: FromWorkflowRun | None = None,
        trace_id: str | None = None,
    ) -> Session:
        """Create a session and its default thread, then write `session.opened`.

        The Event envelope requires `thread_id`; for the first event
        in the file (`session.opened`) that id must point at a real
        thread. Today one thread per session is the rule, so
        `create_session` allocates the default thread as part of
        session creation. Additional threads are created explicitly
        via `create_thread` and remain a no-event operation.

        Args:
            initial_agent_id: Agent ref the session opens against.
            name: Display name; defaults to the new session id.
            from_workflow_run: Parent pointer set by the agent runner
                when an `agent:` workflow node spawns the session.
                When non-`None` it populates `Session.from_workflow_run`
                AND injects a `spawned_by` entry into the
                `session.opened` event's `data` payload per the
                [workflow event log](../explanation/workflow-event-log.md).
                Chat- and API-spawned sessions leave it `None` and the
                payload is byte-identical to the pre-spawn shape.
            trace_id: Stamped on the `session.opened` event envelope
                so the spawned session's very first event correlates
                with the workflow run that spawned it. Every subsequent
                event the engine emits inherits the same trace id via
                [`ExecutionContext`][engine.context.ExecutionContext]
                (the engine's `_emit` chokepoint stamps it on every
                emitted Event). Chat and API callers leave it `None`
                and the envelope field stays `None`.

        Returns:
            The newly created [`Session`][schemas.entities.Session].
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
            trace_id=trace_id,
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
        """Return the session record for `session_id`.

        Raises:
            SessionNotFound: No record exists for that id.
        """
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
        """Return session records filtered and ordered per the kwargs.

        Args:
            starred: Filter to starred (`True`) or un-starred (`False`)
                sessions, or both (`None`).
            archived: Same filter shape on the archived flag.
            limit: Cap the result count. `None` returns every match.
            order_by: Column to sort on.
            order: Ascending or descending.
        """
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
        """Patch the mutable UI fields on a session record.

        Only the kwargs that are not `None` are applied; the rest of
        the record is left untouched. The backend re-stamps
        `updated_at`, so the returned record is refetched to give the
        caller the authoritative timestamp.

        Args:
            session_id: The session to patch.
            name: New display name; `None` leaves it unchanged.
            starred: New starred flag; `None` leaves it unchanged.
            archived: New archived flag; `None` leaves it unchanged.

        Returns:
            The post-update session record.

        Raises:
            SessionNotFound: No record exists for `session_id`.
        """
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
        """Delete the session record, JSONL file, and lock sidecar.

        Idempotent — missing files and missing records are silently
        tolerated.
        """
        await self._backend.delete_session(session_id)
        await asyncio.to_thread(safe_unlink, jsonl_path(self._root, session_id))
        await asyncio.to_thread(safe_unlink, lock_path(self._root, session_id))

    # --- Thread lifecycle ----------------------------------------------

    async def create_thread(
        self,
        session_id: SessionId,
        agent_id: AgentId,
    ) -> Thread:
        """Create a new thread record under `session_id`.

        Confirms the session exists before inserting; no event is
        written (thread creation is a record-only operation in this
        phase).

        Raises:
            SessionNotFound: No session record exists for `session_id`.
        """
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
        """Return the thread record for `thread_id`.

        Raises:
            ThreadNotFound: No record exists for that id.
        """
        result = await self._backend.get_thread(thread_id)
        if result is None:
            raise ThreadNotFound(thread_id=str(thread_id))
        return result

    async def list_threads(self, session_id: SessionId) -> list[Thread]:
        """Return every thread record belonging to `session_id`."""
        return await self._backend.list_threads(session_id)

    # --- Event operations ----------------------------------------------

    async def append_event(
        self,
        thread_id: ThreadId,
        event: Event,
    ) -> Event:
        """Append `event` to the JSONL backing `thread_id`'s session.

        Resolves the thread → session mapping, validates the caller's
        `session_id` matches the resolved one, then delegates to
        `append_event_sync` under a worker thread so the event loop
        stays unblocked.

        Args:
            thread_id: The thread the event belongs to.
            event: The event to append. `parent_event_id` is filled
                in by the store; callers leave it `None`.

        Returns:
            The appended event with `parent_event_id` populated.

        Raises:
            ThreadNotFound: No thread record exists for `thread_id`.
            ValueError: `event.session_id` does not match the resolved
                session — coercing would mask a real engine bug.
            StoreWriteFailed: The append failed at the filesystem.
        """
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
        """Return the most recent event on `thread_id`, or `None`.

        Recovers any partial trailing line on the file before scanning
        so a previous crash does not skew the leaf result.
        """
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
        tail: int | None = None,
    ) -> AsyncIterator[Event]:
        """Yield events for `thread_id` in file order.

        Reads all matching lines on a worker thread up front so the
        async iteration itself never blocks the loop. Filters to
        `thread_id`: today there is one thread per session and the
        filter is the identity, but the same call site stays correct
        when multi-thread sessions arrive.

        Args:
            thread_id: The thread to read.
            from_event_id: When set, yield only events strictly after
                the event with this id. Unknown ids yield nothing (per
                the pre-spec-003 shape). spec-003's `EventAdapter`
                treats an unknown-id bookmark as "replay everything";
                it detects the unknown case by checking the bookmark
                against the events it read separately.
            limit: Cap the yield count. `None` yields every match.
            tail: When set, only consider the LAST `tail` events of
                the log for this thread (applied BEFORE the
                `from_event_id` filter). `None` = full log. Added in
                spec-003 for the prompt-mid-crash scan window bound.

        Yields:
            Each [`Event`][schemas.events.Event] in file order.

        Raises:
            ThreadNotFound: No thread record exists for `thread_id`.
            StoreCorruptLog: The JSONL has a malformed mid-file line.
        """
        thread = await self.get_thread(thread_id)
        path = jsonl_path(self._root, thread.session_id)
        await asyncio.to_thread(recover_partial_final_line, path)

        # Collect the lines on a worker thread so the iteration itself
        # is non-blocking even though disk I/O happens up front.
        all_events: list[Event] = await asyncio.to_thread(
            lambda: list(read_lines(path))
        )

        # spec-003: `tail` applies first (before `from_event_id`) so
        # callers combining both (bookmark + bounded window) get the
        # intersection: "last N events, but only those after
        # bookmark".
        if tail is not None:
            all_events = all_events[-tail:] if tail > 0 else []

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
            # Filter to the requested thread. Today's one-thread-per-
            # session shape makes this identity, but the filter is the
            # forward-compatible behaviour for multi-thread.
            if event.thread_id == thread_id:
                yield event

    async def read_path_to_leaf(
        self,
        thread_id: ThreadId,
    ) -> AsyncIterator[Event]:
        """Yield the events on the path from the thread's root to its leaf.

        Today's no-branching event log makes file order the path, so
        this delegates to `read_events`. A future branching-aware
        implementation replaces this body without changing the call
        site.
        """
        # No branching → file order is the path. The branching-aware
        # implementation replaces this body without changing the call
        # site.
        async for event in self.read_events(thread_id):
            yield event

    async def has_any_event_of_type(
        self,
        thread_id: ThreadId,
        event_type: EventType,
    ) -> bool:
        """Return True if `thread_id` has at least one event of `event_type`.

        Cheap presence-check that short-circuits at the JSONL level —
        `read_lines` is a lazy generator, so this stops parsing on
        the first match. Preferred over
        ``any(e.type == t for e in read_path_to_leaf(...))`` when the
        caller only needs presence, not the events themselves:
        ``read_events`` materialises the full log as a list up-front
        (for async-iteration safety) and paying that read on every
        chat-open just to detect a single event type is wasteful.

        qol-100 introduced this to restore the pre-bug-098 baseline
        for the eager compaction path: on a wedged provider (no
        declared ``context_length`` + no cross-provider fallback), a
        fresh session with no user activity now bails on a cheap
        presence-check instead of reading the full log.

        Args:
            thread_id: The thread to probe.
            event_type: The event type to look for.

        Returns:
            True on the first matching event, False if the log is
            empty or no matching event exists.

        Raises:
            ThreadNotFound: No thread record exists for `thread_id`.
            StoreCorruptLog: The JSONL has a malformed mid-file line.
        """
        thread = await self.get_thread(thread_id)
        path = jsonl_path(self._root, thread.session_id)
        await asyncio.to_thread(recover_partial_final_line, path)

        def _probe() -> bool:
            for event in read_lines(path):
                if event.thread_id == thread_id and event.type == event_type:
                    return True
            return False

        return await asyncio.to_thread(_probe)

    async def get_last_logical_seq(self, session_id: SessionId) -> int:
        """Return the `logical_seq` on the session's JSONL tail, or 0.

        Test-facing helper. Delegates to
        `_read_last_logical_seq`
        under a worker thread so the event loop stays unblocked.

        spec-003 uses this in `test_logical_seq_monotonic.py` to
        verify the next append will get `tail + 1`.
        """
        from store.jsonl import _read_last_logical_seq  # noqa: PLC0415

        path = jsonl_path(self._root, session_id)
        return await asyncio.to_thread(_read_last_logical_seq, path)

    # --- Session meta.json ---------------------------------------------

    async def read_meta(self, session_id: SessionId) -> SessionMeta:
        """Read the session's `meta.json` sidecar.

        Missing / empty / corrupt files return a default
        [`SessionMeta`][schemas.entities.SessionMeta] rather than
        raising — this is the "legacy meta.json validates cleanly"
        contract from spec-003 AC #9. Extra keys are ignored
        (`model_config extra="ignore"`).

        The file lives at
        `<root>/sessions/<session_id>.meta.json`.
        """
        path = self._meta_path(session_id)
        return await asyncio.to_thread(_read_meta_sync, path)

    async def update_meta(
        self,
        session_id: SessionId,
        *,
        event_adapter_bookmark: EventId | None | _Sentinel = _SENTINEL,
        logical_seq_bookmark: int | _Sentinel = _SENTINEL,
    ) -> SessionMeta:
        """Patch the session's `meta.json` sidecar.

        Reads the existing meta (or a default if none exists), applies
        the non-sentinel kwargs, writes the result back atomically via
        `os.replace`. The write is best-effort — a failed write does
        not raise (the JSONL log is still authoritative for the
        `logical_seq` counter; the bookmark is an optimisation).

        Args:
            session_id: The session to patch.
            event_adapter_bookmark: New bookmark, or `None` to clear.
                Pass `_SENTINEL` (default) to leave unchanged.
            logical_seq_bookmark: Reserved for v1.1. Pass `_SENTINEL`
                (default) to leave unchanged.

        Returns:
            The post-update `SessionMeta`.
        """
        existing = await self.read_meta(session_id)
        updates: dict[str, object] = {}
        if not isinstance(event_adapter_bookmark, _Sentinel):
            updates["event_adapter_bookmark"] = event_adapter_bookmark
        if not isinstance(logical_seq_bookmark, _Sentinel):
            updates["logical_seq_bookmark"] = logical_seq_bookmark
        updated = existing.model_copy(update=updates)
        path = self._meta_path(session_id)
        await asyncio.to_thread(_write_meta_sync, path, updated)
        return updated

    def _meta_path(self, session_id: SessionId) -> Path:
        """Return the meta.json path for `session_id`."""
        return self._root / "sessions" / f"{session_id}.meta.json"

    # --- Content store -------------------------------------------------

    async def write_content(self, content: bytes | str) -> ContentRef:
        """Persist `content` to the content sidecar store.

        Strings are encoded as UTF-8. The write is idempotent: the
        same payload written twice resolves to the same on-disk file.

        Returns:
            A [`ContentRef`][schemas.entities.ContentRef] naming the
            stored payload.

        Raises:
            StoreWriteFailed: The sidecar write failed.
        """
        payload = content if isinstance(content, bytes) else content.encode("utf-8")
        return await asyncio.to_thread(write_content_sync, self._root, payload)

    # --- Reconstruction ------------------------------------------------

    async def reconstruct_from_filesystem(self) -> ReconstructionResult:
        """Rebuild missing session and thread records from the JSONL files.

        Scans `<root>/sessions/*.jsonl`, reads each file's
        `session.opened` event to recover the agent id and creation
        timestamp, and inserts a record only when the backend is
        missing one. Files without a valid `session.opened` are
        skipped with a warning rather than aborting the whole sweep.

        Returns:
            A [`ReconstructionResult`][store.ReconstructionResult]
            with counts of found versus reconstructed sessions and
            threads plus any per-file warnings.
        """
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
                    # UI-state defaults — reconstruct can't recover
                    # operator-set name / starred / archived.
                    name=session_id,
                    starred=False,
                    archived=False,
                    created_at=now_ts,
                    updated_at=now_ts,
                )
                await self._backend.insert_session(rebuilt)
                sessions_reconstructed += 1

            # Scan the file's events for threads referenced. We use
            # the inferred (session, agent_id) since there is no
            # explicit thread.created event today.
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


def _read_meta_sync(path: Path) -> SessionMeta:
    """Read `path` as a `SessionMeta`, returning defaults on any error.

    Called from `SessionStore.read_meta` under `asyncio.to_thread`.
    Legacy files (missing fields, extra unknown fields) validate
    cleanly because both fields default and `model_config extra="ignore"`.
    Missing / corrupt files return a default meta.
    """
    if not path.exists():
        return SessionMeta()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return SessionMeta()
    if not raw.strip():
        return SessionMeta()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return SessionMeta()
    if not isinstance(payload, dict):
        return SessionMeta()
    try:
        return SessionMeta.model_validate(payload)
    except ValidationError:
        return SessionMeta()


def _write_meta_sync(path: Path, meta: SessionMeta) -> None:
    """Atomically write `meta` to `path` via a temp file + `os.replace`.

    Best-effort — silently swallows OSError so the caller (typically
    `EventAdapter._persist_bookmark`) doesn't have to guard against
    disk full / permission errors mid-replay. The JSONL log remains
    authoritative for the events themselves; the meta.json is an
    optimisation.
    """
    import os as _os  # noqa: PLC0415 — local to avoid extra module-scope import

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(meta.model_dump_json(), encoding="utf-8")
        _os.replace(tmp, path)
    except OSError:
        # Best-effort — bookmark drift is tolerated. See spec-003
        # coordinator decision #1: JSONL tail is authoritative.
        return


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _safe_read_events(path: Path) -> list[Event]:
    """Best-effort event read for reconstruct; corruption returns `[]`."""
    try:
        return list(read_lines(path))
    except Exception:  # noqa: BLE001 — reconstruct is intentionally tolerant
        return []
