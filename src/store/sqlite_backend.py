"""SQLite implementation of :class:`store.backend.EntityBackend`.

Default v1 backend per handbook §6.1 — zero external services, single
file at ``<platform-root>/.platform/entities.db`` (path passed in by
the caller).

WAL mode is enabled so multiple processes can read while a writer
holds the database. The single-writer constraint of SQLite is fine
for v1: ``insert_session`` and friends are rare relative to event
appends (which never touch this DB).

Sync I/O is wrapped in :func:`asyncio.to_thread` so the event loop
stays unblocked (spec §I10).
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from schemas import AgentId, Session, SessionId, Thread, ThreadId

if TYPE_CHECKING:
    from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    starred     INTEGER NOT NULL DEFAULT 0,
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS threads (
    thread_id   TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    agent_id    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_threads_session
    ON threads(session_id);
"""

_ORDER_COLS: dict[str, str] = {"created_at": "created_at", "name": "name"}
_ORDER_DIRS: dict[str, str] = {"asc": "ASC", "desc": "DESC"}


def _row_to_session(row: sqlite3.Row) -> Session:
    return Session(
        session_id=SessionId(row["session_id"]),
        agent_id=AgentId(row["agent_id"]),
        name=row["name"],
        starred=bool(row["starred"]),
        archived=bool(row["archived"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_thread(row: sqlite3.Row) -> Thread:
    return Thread(
        thread_id=ThreadId(row["thread_id"]),
        session_id=SessionId(row["session_id"]),
        agent_id=AgentId(row["agent_id"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


class SqliteBackend:
    """Spec-conformant SQLite-backed :class:`EntityBackend`.

    Path is supplied at construction; the file is created on first
    :meth:`initialize`. Connection is opened with
    ``check_same_thread=False`` so :func:`asyncio.to_thread` workers
    can use it; serialised access is enforced by a single
    :class:`asyncio.Lock` held during every mutating operation.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None
        # asyncio.Lock serialises mutations across coroutines; the
        # SQLite connection is shared and not thread-safe without this.
        self._lock = asyncio.Lock()

    # --- lifecycle -----------------------------------------------------

    async def initialize(self) -> None:
        if self._conn is not None:
            return
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self._path,
            check_same_thread=False,
            isolation_level=None,  # autocommit-style; we manage txns
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SCHEMA)
        self._conn = conn

    async def close(self) -> None:
        if self._conn is None:
            return
        conn = self._conn
        self._conn = None
        await asyncio.to_thread(conn.close)

    def _require_conn(self) -> sqlite3.Connection:
        """Return the active connection or raise if uninitialised.

        Sync sites narrow the optional connection by going through
        this helper rather than ``assert``, so the binary is robust
        when stripped (``python -O``) and the failure mode is a
        clear ``RuntimeError`` rather than ``AssertionError``.
        """
        if self._conn is None:
            msg = "SqliteBackend used before initialize() / after close()"
            raise RuntimeError(msg)
        return self._conn

    # --- sessions ------------------------------------------------------

    async def insert_session(self, session: Session) -> None:
        async with self._lock:
            await asyncio.to_thread(self._insert_session_sync, session)

    def _insert_session_sync(self, session: Session) -> None:
        conn = self._require_conn()
        conn.execute(
            "INSERT INTO sessions "
            "(session_id, agent_id, name, starred, archived, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session.session_id,
                session.agent_id,
                session.name,
                int(session.starred),
                int(session.archived),
                session.created_at.isoformat(),
                session.updated_at.isoformat(),
            ),
        )

    async def get_session(self, session_id: SessionId) -> Session | None:
        # SQLite's connection isn't thread-safe even for concurrent
        # reads when shared across `to_thread` workers; serialise
        # every access (incl. reads) through the asyncio lock.
        async with self._lock:
            return await asyncio.to_thread(self._get_session_sync, session_id)

    def _get_session_sync(self, session_id: SessionId) -> Session | None:
        conn = self._require_conn()
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return _row_to_session(row) if row else None

    async def list_sessions(
        self,
        *,
        starred: bool | None = None,
        archived: bool | None = None,
        limit: int | None = None,
        order_by: Literal["created_at", "name"] = "created_at",
        order: Literal["asc", "desc"] = "desc",
    ) -> list[Session]:
        async with self._lock:
            return await asyncio.to_thread(
                self._list_sessions_sync,
                starred=starred,
                archived=archived,
                limit=limit,
                order_by=order_by,
                order=order,
            )

    def _list_sessions_sync(
        self,
        *,
        starred: bool | None,
        archived: bool | None,
        limit: int | None,
        order_by: Literal["created_at", "name"],
        order: Literal["asc", "desc"],
    ) -> list[Session]:
        conn = self._require_conn()
        clauses: list[str] = []
        params: list[Any] = []
        if starred is not None:
            clauses.append("starred = ?")
            params.append(int(starred))
        if archived is not None:
            clauses.append("archived = ?")
            params.append(int(archived))
        # Whitelisted ORDER BY column + direction. The Literal types
        # guard at the caller boundary; the lookup is the runtime
        # belt + braces so f-string interpolation can't smuggle in
        # SQL even if a future caller bypasses the type check.
        order_col = _ORDER_COLS[order_by]
        direction = _ORDER_DIRS[order]
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM sessions {where} "  # noqa: S608 — whitelisted fragments
            f"ORDER BY {order_col} {direction}"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [_row_to_session(row) for row in conn.execute(sql, params).fetchall()]

    async def update_session(self, session: Session) -> None:
        # We re-stamp updated_at at the store layer so this backend
        # method takes the fully-formed Session and just persists.
        session = session.model_copy(update={"updated_at": datetime.now(UTC)})
        async with self._lock:
            await asyncio.to_thread(self._update_session_sync, session)

    def _update_session_sync(self, session: Session) -> None:
        conn = self._require_conn()
        conn.execute(
            "UPDATE sessions SET "
            "name = ?, starred = ?, archived = ?, updated_at = ? "
            "WHERE session_id = ?",
            (
                session.name,
                int(session.starred),
                int(session.archived),
                session.updated_at.isoformat(),
                session.session_id,
            ),
        )

    async def delete_session(self, session_id: SessionId) -> None:
        async with self._lock:
            await asyncio.to_thread(self._delete_session_sync, session_id)

    def _delete_session_sync(self, session_id: SessionId) -> None:
        conn = self._require_conn()
        # ON DELETE CASCADE removes threads automatically.
        conn.execute(
            "DELETE FROM sessions WHERE session_id = ?",
            (session_id,),
        )

    # --- threads -------------------------------------------------------

    async def insert_thread(self, thread: Thread) -> None:
        async with self._lock:
            await asyncio.to_thread(self._insert_thread_sync, thread)

    def _insert_thread_sync(self, thread: Thread) -> None:
        conn = self._require_conn()
        conn.execute(
            "INSERT INTO threads "
            "(thread_id, session_id, agent_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                thread.thread_id,
                thread.session_id,
                thread.agent_id,
                thread.created_at.isoformat(),
            ),
        )

    async def get_thread(self, thread_id: ThreadId) -> Thread | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_thread_sync, thread_id)

    def _get_thread_sync(self, thread_id: ThreadId) -> Thread | None:
        conn = self._require_conn()
        row = conn.execute(
            "SELECT * FROM threads WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        return _row_to_thread(row) if row else None

    async def list_threads(self, session_id: SessionId) -> list[Thread]:
        async with self._lock:
            return await asyncio.to_thread(self._list_threads_sync, session_id)

    def _list_threads_sync(self, session_id: SessionId) -> list[Thread]:
        conn = self._require_conn()
        rows = conn.execute(
            "SELECT * FROM threads WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        return [_row_to_thread(r) for r in rows]

    # --- reconstruction helper -----------------------------------------

    async def delete_all(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._delete_all_sync)

    def _delete_all_sync(self) -> None:
        conn = self._require_conn()
        conn.execute("DELETE FROM threads")
        conn.execute("DELETE FROM sessions")


__all__ = ["SqliteBackend"]
