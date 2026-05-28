"""Store — session/run entity records and the JSONL event log.

Per Module API Reference §3. The filesystem JSONL is ground truth;
entity records (SQLite default, Postgres scale-up) are rebuildable
from the log via :meth:`SessionStore.reconstruct_from_filesystem`.

Public surface:

- :class:`SessionStore` — the public API consumed by the engine + CLI.
- :class:`EntityBackend` — the Protocol every entity backend implements.
- :class:`SqliteBackend` — the default v1 implementation.
- :class:`ReconstructionResult` — return type of
  :meth:`SessionStore.reconstruct_from_filesystem`.
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
