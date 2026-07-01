"""The `EntityBackend` Protocol — pluggable mutable-record store.

The backend owns the mutable [`Session`][schemas.entities.Session] and
[`Thread`][schemas.entities.Thread] entity records. The JSONL event
log is filesystem-only and managed by `store.jsonl`; the backend
never touches it.

v1 ships one implementation
([`SqliteBackend`][store.sqlite_backend.SqliteBackend]). The Protocol
exists so a future Postgres backend drops in at the same surface
without touching
[`SessionStore`][store.session_store.SessionStore].

All methods are `async`. Implementations MUST NOT block the event
loop on I/O; sync libraries (SQLite, psycopg) are wrapped in
`asyncio.to_thread`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from schemas import Session, SessionId, Thread, ThreadId


class EntityBackend(Protocol):
    """Contract for the mutable-entity record store.

    Implementations persist the [`Session`][schemas.entities.Session]
    and [`Thread`][schemas.entities.Thread] records the store hands
    out and never touch the JSONL event log. Every method is `async`
    so backends can wrap blocking client libraries in
    `asyncio.to_thread` without forcing the caller to know.
    """

    async def initialize(self) -> None:
        """Create tables or open the connection. Idempotent."""

    async def close(self) -> None:
        """Release connections and handles. Idempotent."""

    # --- Sessions ------------------------------------------------------

    async def insert_session(self, session: Session) -> None:
        """Persist a freshly created session record."""

    async def get_session(self, session_id: SessionId) -> Session | None:
        """Return the session record, or `None` when it does not exist."""

    async def list_sessions(
        self,
        *,
        starred: bool | None = None,
        archived: bool | None = None,
        limit: int | None = None,
        order_by: Literal["created_at", "name"] = "created_at",
        order: Literal["asc", "desc"] = "desc",
    ) -> list[Session]:
        """Return a filtered, ordered list of session records.

        Args:
            starred: Include only starred sessions when `True`, only
                un-starred when `False`, both when `None`.
            archived: Same filter shape as `starred`, on the archived
                flag.
            limit: Cap the result count. `None` returns every match.
            order_by: Column to sort on.
            order: Ascending or descending.
        """

    async def update_session(self, session: Session) -> None:
        """Persist mutable-field changes; `session` is authoritative."""

    async def delete_session(self, session_id: SessionId) -> None:
        """Delete the session record. Idempotent — missing id is a no-op."""

    # --- Threads -------------------------------------------------------

    async def insert_thread(self, thread: Thread) -> None:
        """Persist a freshly created thread record."""

    async def get_thread(self, thread_id: ThreadId) -> Thread | None:
        """Return the thread record, or `None` when it does not exist."""

    async def list_threads(self, session_id: SessionId) -> list[Thread]:
        """Return every thread record belonging to `session_id`."""

    # --- Reconstruction helpers ----------------------------------------

    async def delete_all(self) -> None:
        """Wipe every session and thread record.

        Used by
        [`SessionStore.reconstruct_from_filesystem`][store.session_store.SessionStore.reconstruct_from_filesystem]
        regression tests and by operators performing a deliberate
        restart; the JSONL files (ground truth) are untouched.
        """


__all__ = ["EntityBackend"]
