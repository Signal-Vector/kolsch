"""Entity records and shared event-envelope sub-types — spec §C + §13.2.

- :class:`Session` and :class:`Thread` are entity records (Schemas §C):
  mutable state held outside the JSONL event log.
- :class:`Actor` is the ``actor`` field of every :class:`Event`
  (Module API Reference §13.2).
- :class:`ContentRef` references the content sidecar store for
  oversized event payloads (Module API Reference §3.2).

These are pure data models — no behaviour beyond Pydantic validation.

Spec conflict (flagged): :class:`Thread` includes ``agent_id`` per
Schemas §C even though Module API Reference §3.2 omits it. §C is
authoritative for entity-record fields, so ``agent_id`` is included.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from schemas.identifiers import AgentId, SessionId, ThreadId

_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")


class Session(BaseModel):
    """A session entity record — Module API Reference §3.2, Schemas §C.

    Mutable UI-organisational fields (``name``, ``starred``,
    ``archived``) sit alongside the immutable identity. The session's
    event log is the JSONL file identified by ``session_id``; this
    record does NOT carry execution history.
    """

    model_config = ConfigDict(frozen=False, extra="forbid")

    session_id: SessionId
    agent_id: AgentId
    name: str
    starred: bool = False
    archived: bool = False
    created_at: datetime
    updated_at: datetime


class Thread(BaseModel):
    """A thread entity record — Schemas §C + Module API Reference §3.2.

    Phase 1 has exactly one thread per session. ``agent_id`` is
    included per Schemas §C (Module API Reference §3.2 omits it; §C is
    authoritative for entity-record shape).
    """

    model_config = ConfigDict(frozen=False, extra="forbid")

    thread_id: ThreadId
    session_id: SessionId
    agent_id: AgentId
    created_at: datetime


class Actor(BaseModel):
    """The actor that emitted an event — §13.2.

    ``kind`` is one of three discrete roles. ``id`` is required for
    ``agent`` and ``user`` actors (so consumers can attribute events);
    for ``system`` actors the id is optional.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["system", "agent", "user"]
    id: str | None = None


class ContentRef(BaseModel):
    """A reference into the content sidecar store — §3.2 + §13.2.

    Oversized event payloads spill to ``content/<hash>.bin``; the event
    keeps an inline truncated form plus this reference.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    hash: str = Field(min_length=64, max_length=64)
    size: int = Field(ge=0)
    stored_at: str

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: object) -> None:
        # Subclassing ContentRef is not part of the contract; this
        # hook exists so a subclass would still inherit validation.
        super().__pydantic_init_subclass__(**kwargs)

    def model_post_init(self, _context: object) -> None:
        if not _SHA256_HEX.fullmatch(self.hash):
            msg = (
                f"ContentRef.hash must be a 64-char SHA-256 hex digest; "
                f"got {self.hash!r}"
            )
            raise ValueError(msg)
