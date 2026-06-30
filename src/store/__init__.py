"""Store — session and thread entity records plus the JSONL event log.

Implements the persistence layer per the
[session/run store contract](../../docs/explanation/session-run-store-contract.md).
The filesystem JSONL is ground truth; entity records (SQLite default,
Postgres scale-up) are rebuildable from the log via
[`SessionStore.reconstruct_from_filesystem`][store.SessionStore.reconstruct_from_filesystem].
"""

from store.backend import EntityBackend
from store.session_store import ReconstructionResult, SessionStore
from store.sqlite_backend import SqliteBackend

__all__ = [
    "EntityBackend",
    "ReconstructionResult",
    "SessionStore",
    "SqliteBackend",
]
