"""Session view mirror for the MCP operator server.

Mirrors the 7-field `_SessionView` projection from `src/api/routes.py`
(``session_id``, ``agent_id``, ``name``, ``starred``, ``archived``,
``created_at``, ``updated_at``) so ``list_sessions`` returns dicts
byte-identical to those of `GET /sessions`. The FastAPI-side model
drops the 8th field `Session.from_workflow_run` — that omission is
load-bearing here too: leaking the parent workflow-run pointer over
MCP would newly widen the read surface.

Why a mirror rather than an import from `src/api/`: keeps `src/api/`
untouched (additive posture — the operator server does not modify the
FastAPI surface). Phase 2, once a second read tool arrives, promotes
`_SessionView` to a shared `src/api/views.py` and both surfaces
import from there.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from schemas import Session


class _SessionView(BaseModel):
    """7-field projection of `Session` — mirror of `api.routes._SessionView`.

    Kept in step with the FastAPI-side model. If a field is added or
    dropped there, mirror it here and update `session_view` below.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    agent_id: str
    name: str
    starred: bool
    archived: bool
    created_at: str
    updated_at: str


def session_view(session: Session) -> dict[str, Any]:
    """Return the 7-field JSON-ready session dict for MCP transport.

    `_SessionView.model_dump(mode="json")` is load-bearing: it coerces
    the underlying `datetime` fields (``created_at`` / ``updated_at``)
    to ISO-8601 strings so `json.dumps(...)` on the resulting dict
    doesn't raise `TypeError: Object of type datetime is not JSON
    serializable`. A naive `session.model_dump()` (or dropping
    `mode="json"`) would preserve `datetime` objects and break the
    caller.

    Two-step: dump the source `Session` to a JSON-ready dict
    (also coercing datetimes to strings + dropping the 8th field
    via `_SessionView`'s `extra="forbid"`), then validate + dump
    through the mirror. Matches the FastAPI-side `_session_view` at
    `src/api/routes.py:361-371`.

    Args:
        session: The store-returned :class:`schemas.entities.Session`.

    Returns:
        A dict with exactly the 7 mirrored fields — no
        `from_workflow_run`, no `datetime` values.
    """
    data = session.model_dump(mode="json")
    view = _SessionView(
        session_id=data["session_id"],
        agent_id=data["agent_id"],
        name=data["name"],
        starred=data["starred"],
        archived=data["archived"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )
    return view.model_dump(mode="json")
