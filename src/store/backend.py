"""EntityBackend Protocol — spec §0 (entity backend is pluggable).

The backend owns the mutable :class:`Session` and :class:`Thread`
entity records. The JSONL event log is filesystem-only and managed by
:mod:`store.jsonl`; the backend never touches it.

v1 ships one implementation (:class:`store.sqlite_backend.SqliteBackend`).
The Protocol exists so a future Postgres implementation drops in at
the same surface without touching :class:`store.session_store.SessionStore`.

All methods are ``async``. Per spec I10, implementations MUST NOT
block the event loop on I/O; sync libraries (SQLite, psycopg) are
wrapped in :func:`asyncio.to_thread`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from schemas import Session, SessionId, Thread, ThreadId


class EntityBackend(Protocol):
    """The mutable-entity store contract."""

    async def initialize(self) -> None:
        """Create tables / open the connection. Idempotent."""

    async def close(self) -> None:
        """Release connections + handles. Idempotent."""

    # --- Sessions ------------------------------------------------------

    async def insert_session(self, session: Session) -> None:
        """Persist a freshly created :class:`Session`."""

    async def get_session(self, session_id: SessionId) -> Session | None:
        """Return the record or ``None`` if it does not exist."""

    async def list_sessions(
        self,
        *,
        starred: bool | None = None,
        archived: bool | None = None,
        limit: int | None = None,
        order_by: Literal["created_at", "name"] = "created_at",
        order: Literal["asc", "desc"] = "desc",
    ) -> list[Session]:
        """Filtered list of sessions per spec §2.1."""

    async def update_session(self, session: Session) -> None:
        """Persist the mutable-field changes; ``session`` is authoritative."""

    async def delete_session(self, session_id: SessionId) -> None:
        """Idempotent — deleting a non-existent session is a no-op."""

    # --- Threads -------------------------------------------------------

    async def insert_thread(self, thread: Thread) -> None: ...

    async def get_thread(self, thread_id: ThreadId) -> Thread | None: ...

    async def list_threads(self, session_id: SessionId) -> list[Thread]: ...

    # --- Reconstruction helpers ----------------------------------------

    async def delete_all(self) -> None:
        """Wipe every session and thread record.

        Used by :meth:`store.SessionStore.reconstruct_from_filesystem`
        regression tests and by operators doing a deliberate restart;
        the JSONL files (ground truth) are untouched.
        """


__all__ = ["EntityBackend"]
