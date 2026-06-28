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
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from schemas.identifiers import AgentId, EventId, SessionId, ThreadId

if TYPE_CHECKING:
    # Forward-referenced from string annotations on the Pydantic
    # models below. ``workflows.ids`` is imported lazily in
    # :func:`_resolve_forward_refs` to break the import cycle:
    #
    #   workflows.ids → schemas.identifiers → schemas/__init__.py
    #     → schemas.entities → workflows.ids
    #
    # The cycle existed since Phase 3.2's :class:`FromWorkflowRun`;
    # the lazy resolution defers the actual rebind until both modules
    # are fully loaded.
    from workflows.ids import InvocationId, WorkflowRunId

_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")


class FromWorkflowRun(BaseModel):
    """Parent pointer on sessions spawned by workflow agent nodes.

    Populated by ``AgentRunner`` in Phase 3.2; surfaces in the
    ``session.opened`` event's ``spawned_by`` payload per
    ``docs/explanation/workflow-event-log.md`` line 62.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # String forward refs — see ``_resolve_forward_refs`` below for
    # the lazy import that breaks the schemas↔workflows.ids cycle.
    workflow_run_id: "WorkflowRunId"
    invocation_id: "InvocationId"
    event_id: EventId  # workflow.node.started event that spawned this session


class ParentRunPointer(BaseModel):
    """Phase 3.7 — parent-workflow pointer on child workflow runs.

    Mirrors :class:`FromWorkflowRun` (Phase 3.2's agent-session
    pointer). The shape is intentionally identical so cross-JSONL
    forensics tools can handle both uniformly: ``workflow_run_id``
    identifies the spawning workflow run, ``invocation_id`` the
    specific ``kolsch/nodes/workflow`` node-invocation inside it.

    Set by :class:`workflows.builtins.workflow_sub.WorkflowSubRunner`
    (Phase 3.7 Step 1) when minting a child run; persisted on the
    child's ``WorkflowRun.parent`` field and round-tripped through
    ``runs/<wrk_id>/meta.json``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # String forward refs — see ``_resolve_forward_refs`` below.
    workflow_run_id: "WorkflowRunId"
    invocation_id: "InvocationId"


def _resolve_forward_refs() -> None:
    """Rebind the ``WorkflowRunId`` / ``InvocationId`` forward refs.

    The cycle:
    ``workflows.ids → schemas.identifiers → schemas/__init__.py →
    schemas.entities → workflows.ids`` (mid-init).
    Defining the Pydantic fields as string forward refs above and
    calling ``model_rebuild`` AFTER ``workflows.ids`` has finished
    loading breaks the cycle without changing the on-disk schema.

    Called once at module-import time below; safe because Python's
    import machinery serialises module loads — when ``schemas`` is
    fully loaded (the import that triggered this module's load has
    returned), ``workflows.ids`` is also fully loaded.
    """
    import workflows.ids as _wf_ids  # noqa: PLC0415

    namespace = {
        "WorkflowRunId": _wf_ids.WorkflowRunId,
        "InvocationId": _wf_ids.InvocationId,
    }
    FromWorkflowRun.model_rebuild(_types_namespace=namespace)
    ParentRunPointer.model_rebuild(_types_namespace=namespace)


# The module-level call deliberately swallows ``ImportError`` so the
# initial cycle-busting load (when ``workflows.ids`` triggers
# ``schemas/__init__.py`` mid-init) doesn't crash; the eager retry
# happens via :func:`_eager_resolve_when_workflows_loaded` below.
try:
    _resolve_forward_refs()
except (ImportError, AttributeError):
    # Pre-cycle: workflows.ids hasn't finished loading yet. The retry
    # at the bottom of ``workflows.ids`` calls back into us once it's
    # done.
    pass


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
    from_workflow_run: FromWorkflowRun | None = None


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

    hash: str
    size: int = Field(ge=0)
    stored_at: str

    @field_validator("hash")
    @classmethod
    def _validate_sha256_hex(cls, v: str) -> str:
        # Using a field_validator (rather than model_post_init) so the
        # failure surfaces as Pydantic's `ValidationError`, consistent
        # with every other constraint on the model. The regex enforces
        # BOTH the 64-character length AND the hex character set in a
        # single check, with one actionable error message.
        if not _SHA256_HEX.fullmatch(v):
            msg = f"ContentRef.hash must be a 64-char SHA-256 hex digest; got {v!r}"
            raise ValueError(msg)
        return v
