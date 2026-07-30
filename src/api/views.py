"""Shared session-view projection for the FastAPI + MCP surfaces.

Promoted from :mod:`api.routes` (was a module-local `_SessionView`
class + `_session_view` helper) so both the HTTP surface and the MCP
operator server (`src/mcp_server/server.py`) render sessions through a
single source of truth. Prevents the two surfaces drifting on the
7-field response contract as more read tools land.

Fields on ``_SessionView`` (Pydantic model with ``extra="forbid"``):
``session_id``, ``agent_id``, ``name``, ``starred``, ``archived``,
``created_at``, ``updated_at``. The 8th field on
:class:`schemas.entities.Session` — ``from_workflow_run`` — is
deliberately **excluded**. Leaking the parent workflow-run pointer
would widen the read surface for both HTTP and MCP callers.

Design notes:

- ``session_view`` returns the :class:`_SessionView` model (not a
  ``dict``). The HTTP handlers use it as-is for FastAPI's
  ``response_model=`` declaration; MCP callers add
  ``.model_dump(mode="json")`` at the callsite to obtain the JSON-ready
  dict they then feed to ``json.dumps``. ``mode="json"`` is
  load-bearing: without it the underlying ``datetime`` fields
  (``created_at`` / ``updated_at``) round-trip as ``datetime`` objects
  and ``json.dumps`` raises ``TypeError: Object of type datetime is
  not JSON serializable``.
- Both surfaces import from this module; nothing here imports from
  ``api.routes``. A one-arrow import graph avoids any
  partially-initialised-module cycle at load time (the reason we moved
  the class alongside the function — leaving the class in ``routes.py``
  and importing it back into ``views.py`` for the return annotation
  would create the exact ``ImportError: cannot import name
  '_SessionView' from partially initialized module 'api.routes'`` that
  spec-111 documents).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from schemas import Session


class _SessionView(BaseModel):
    """7-field JSON-ready projection of :class:`schemas.entities.Session`.

    Used as FastAPI's ``response_model=`` for the ``/sessions`` handlers
    AND (via ``session_view(...).model_dump(mode="json")``) as the wire
    shape of the MCP ``list_sessions`` tool. Kept in step by living in
    a single module.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    agent_id: str
    name: str
    starred: bool
    archived: bool
    created_at: str
    updated_at: str


def session_view(session: Session) -> _SessionView:
    """Return the 7-field :class:`_SessionView` projection of ``session``.

    Two-step: dump the source :class:`~schemas.entities.Session` to a
    JSON-ready dict (coerces ``created_at`` / ``updated_at`` to
    ISO-8601 strings) then re-validate through :class:`_SessionView`'s
    ``extra="forbid"`` which drops the 8th field
    (``from_workflow_run``) alongside any other future additions to the
    source model.

    Callers:

    - FastAPI handlers use the returned model directly (FastAPI
      serializes it via its own ``response_model=`` machinery).
    - MCP tools do ``session_view(s).model_dump(mode="json")`` at the
      callsite — the ``mode="json"`` step is what makes the datetime
      fields JSON-serializable for the subsequent ``json.dumps``.

    Args:
        session: The store-returned session record.

    Returns:
        A :class:`_SessionView` carrying exactly the 7 mirrored fields.
    """
    data = session.model_dump(mode="json")
    return _SessionView(
        session_id=data["session_id"],
        agent_id=data["agent_id"],
        name=data["name"],
        starred=data["starred"],
        archived=data["archived"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )
