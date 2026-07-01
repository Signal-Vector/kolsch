"""Entity records and shared event-envelope sub-types.

Pure data models, no behaviour beyond Pydantic validation:

- [`Session`][schemas.entities.Session] and
  [`Thread`][schemas.entities.Thread] — entity records (mutable state
  held outside the JSONL event log).
- [`Actor`][schemas.entities.Actor] — the `actor` field stamped on
  every event: which side of the conversation produced this record
  (`system`, `agent`, or `user`).
- [`ContentRef`][schemas.entities.ContentRef] — pointer into the
  content sidecar store for oversized event payloads that spill out
  of the JSONL.
- [`FromWorkflowRun`][schemas.entities.FromWorkflowRun],
  [`ParentRunPointer`][schemas.entities.ParentRunPointer], and
  [`ForkedFrom`][schemas.entities.ForkedFrom] — three parent-pointer
  shapes for cross-JSONL forensics (session-spawned-by-workflow,
  workflow-spawned-by-workflow, run-forked-from-run).

For the narrative on which contracts each record participates in, see
the [Schemas architecture page](../architecture/schemas.md).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from schemas.identifiers import AgentId, EventId, SessionId, ThreadId

if TYPE_CHECKING:
    # Forward-referenced from string annotations on the Pydantic models
    # below. `workflows.ids` is imported lazily in `_resolve_forward_refs`
    # to break the import cycle:
    #
    #   workflows.ids → schemas.identifiers → schemas/__init__.py
    #     → schemas.entities → workflows.ids
    #
    # The cycle exists because `FromWorkflowRun` references types from
    # `workflows.ids`; the lazy resolution defers the actual rebind
    # until both modules are fully loaded.
    from workflows.ids import InvocationId, WorkflowRunId

_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")


class FromWorkflowRun(BaseModel):
    """Parent pointer on sessions spawned by a workflow agent node.

    Populated by the agent runner when a workflow's agent node opens
    a new session; surfaces in the `session.opened` event's
    `spawned_by` payload so cross-JSONL tools can walk from the
    spawned session back to the workflow run that produced it. See
    the [workflow event log](../explanation/workflow-event-log.md).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # String forward refs — see `_resolve_forward_refs` below for the
    # lazy import that breaks the schemas↔workflows.ids cycle.
    workflow_run_id: "WorkflowRunId"
    invocation_id: "InvocationId"
    event_id: EventId  # workflow.node.started event that spawned this session


class ParentRunPointer(BaseModel):
    """Parent-workflow pointer on a child workflow run.

    Mirrors [`FromWorkflowRun`][schemas.entities.FromWorkflowRun] (the
    session-side equivalent). The shape is intentionally identical so
    cross-JSONL forensics tools can handle both uniformly:
    `workflow_run_id` identifies the spawning workflow run,
    `invocation_id` the specific `kolsch/nodes/workflow` node
    invocation inside it.

    Set by
    [`WorkflowSubRunner`][workflows.builtins.workflow_sub.WorkflowSubRunner]
    when minting a child run; persisted on the child's
    `WorkflowRun.parent` field and round-tripped through
    `runs/<wrk_id>/meta.json`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # String forward refs — see `_resolve_forward_refs` below.
    workflow_run_id: "WorkflowRunId"
    invocation_id: "InvocationId"


class ForkedFrom(BaseModel):
    """Parent-run pointer on a forked workflow run.

    Completes the three-pattern set with
    [`ParentRunPointer`][schemas.entities.ParentRunPointer]
    (sub-workflow pointer) and
    [`FromWorkflowRun`][schemas.entities.FromWorkflowRun]
    (agent-session pointer). The three shapes are intentionally
    symmetric so cross-JSONL forensics tools can handle every
    parent-pointer surface uniformly.

    `run_id` identifies the workflow run being forked from;
    `fork_event_id` pins the event in that run's `events.jsonl` at
    which the fork is rooted. A forked run can also carry `parent`
    (if it was originally spawned by a sub-workflow node) — the two
    fields are independent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # String forward refs — see `_resolve_forward_refs` below.
    run_id: "WorkflowRunId"
    fork_event_id: EventId


def _resolve_forward_refs() -> None:
    """Rebind the `WorkflowRunId` and `InvocationId` forward refs.

    The cycle:

    ```
    workflows.ids → schemas.identifiers → schemas/__init__.py
      → schemas.entities → workflows.ids   (mid-init)
    ```

    Defining the Pydantic fields as string forward refs above and
    calling `model_rebuild` after `workflows.ids` has finished loading
    breaks the cycle without changing the on-disk schema.

    Called once at module-import time below; safe because Python's
    import machinery serialises module loads — when `schemas` is
    fully loaded (the import that triggered this module's load has
    returned), `workflows.ids` is also fully loaded.
    """
    import workflows.ids as _wf_ids  # noqa: PLC0415

    namespace = {
        "WorkflowRunId": _wf_ids.WorkflowRunId,
        "InvocationId": _wf_ids.InvocationId,
    }
    FromWorkflowRun.model_rebuild(_types_namespace=namespace)
    ParentRunPointer.model_rebuild(_types_namespace=namespace)
    ForkedFrom.model_rebuild(_types_namespace=namespace)


# The module-level call deliberately swallows `ImportError` so the
# initial cycle-busting load (when `workflows.ids` triggers
# `schemas/__init__.py` mid-init) doesn't crash; the eager retry
# happens via `_eager_resolve_when_workflows_loaded` below.
try:
    _resolve_forward_refs()
except (ImportError, AttributeError):
    # Pre-cycle: workflows.ids hasn't finished loading yet. The retry
    # at the bottom of `workflows.ids` calls back into us once it's
    # done.
    pass


class Session(BaseModel):
    """A session entity record — one conversation instance.

    Mutable UI-organisational fields (`name`, `starred`, `archived`)
    sit alongside the immutable identity (`session_id`, `agent_id`,
    `created_at`). The session's event log is the JSONL file
    identified by `session_id`; this record does NOT carry execution
    history — that lives in the log.
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
    """A thread entity record — the persistence unit inside a session.

    A thread's `thread_id` identifies the JSONL file that stores its
    conversation history. `agent_id` is carried alongside so consumers
    can attribute the thread without crossing the join to its
    [`Session`][schemas.entities.Session].
    """

    model_config = ConfigDict(frozen=False, extra="forbid")

    thread_id: ThreadId
    session_id: SessionId
    agent_id: AgentId
    created_at: datetime


class Actor(BaseModel):
    """The actor stamped on every event — who or what produced it.

    Three discrete roles:

    - `system` — synthetic platform operations.
    - `agent` — a configured agent run.
    - `user` — the human user.

    `id` is required for `agent` and `user` actors so consumers can
    attribute events; for `system` actors it is optional.

    Distinct from the *principal* used in authz checks: an actor
    records "who emitted this event" on the log; a principal is
    "whose permissions are we checking". The same agent run may
    appear as both — the words capture different lenses.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["system", "agent", "user"]
    id: str | None = None


class ContentRef(BaseModel):
    """A reference into the content sidecar store.

    Oversized event payloads spill to `content/<hash>.bin`; the event
    keeps an inline truncated form plus this reference so consumers
    can fetch the full payload only when they need it.
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
