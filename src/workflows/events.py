"""Workflow event envelopes — Phase 3.1 Step 5.

Master spec §11.1 lists the 13 executor-emitted event kinds; §11.2
pins ordering rules; §11.3 names the common-field shape every
envelope carries. This module ships:

- :class:`WorkflowEventKind` — the 13-value StrEnum vocabulary.
- :class:`WorkflowEvent` — common-field base envelope.
- :class:`NodeScopedWorkflowEvent` — adds ``invocation_id`` +
  ``node_name`` at the top level for the 6 ``workflow.node.*`` kinds.
- 13 typed subclasses, each with ``kind: Literal[...]`` so Pydantic
  discriminates on emit. The subclasses are thin — payload remains a
  ``dict[str, Any]`` here; tightening per-kind payload schemas lands
  in Phase 3.2+ as agent / branch / map runtimes mature.
- :func:`new_event_id` — mint a fresh ``ev_<uuid>``.
- :func:`emit_event` — append a typed envelope through the
  :class:`workflows.run_store.WorkflowRunStore` JSONL surface.

The on-disk JSONL line is the envelope's ``model_dump(mode="json")``
output: a single object with the common fields at the top level and
``payload`` carrying kind-specific data. Existing Step 1-4 tests that
inspect ``event["kind"]`` and ``event["payload"]["..."]`` still pass.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from schemas.identifiers import EventId
from workflows.ids import InvocationId, TraceId, WorkflowRunId

if TYPE_CHECKING:
    from workflows.run_store import WorkflowRunStore


__all__ = [
    "NodeScopedWorkflowEvent",
    "PolicyDecisionMadeEvent",
    "WorkflowEvent",
    "WorkflowEventKind",
    "WorkflowNodeCompletedEvent",
    "WorkflowNodeEmittedEvent",
    "WorkflowNodeFailedEvent",
    "WorkflowNodeLoggedEvent",
    "WorkflowNodePrunedEvent",
    "WorkflowNodeStartedEvent",
    "WorkflowNodeTimedOutEvent",
    "WorkflowRunAbortedEvent",
    "WorkflowRunCompletedEvent",
    "WorkflowRunFailedEvent",
    "WorkflowRunStartedEvent",
    "WorkflowRunTimedOutEvent",
    "WorkflowToolCalledEvent",
    "emit_event",
    "new_event_id",
]


class WorkflowEventKind(StrEnum):
    """The executor-emitted event vocabulary — master spec §11.1.

    ``model.request.sent`` is the agent-emitted caller-chain carrier
    per the Phase 3.1 build doc; it lives in the agent runtime
    (Phase 3.2) so this enum covers only the workflow-engine-owned
    kinds. Phase 3.3 adds :attr:`NODE_PRUNED` per master spec §11.1
    extension.
    """

    RUN_STARTED = "workflow.run.started"
    RUN_COMPLETED = "workflow.run.completed"
    RUN_FAILED = "workflow.run.failed"
    RUN_TIMED_OUT = "workflow.run.timed_out"
    RUN_ABORTED = "workflow.run.aborted"
    NODE_STARTED = "workflow.node.started"
    NODE_COMPLETED = "workflow.node.completed"
    NODE_FAILED = "workflow.node.failed"
    NODE_TIMED_OUT = "workflow.node.timed_out"
    NODE_PRUNED = "workflow.node.pruned"
    NODE_LOGGED = "workflow.node.logged"
    NODE_EMITTED = "workflow.node.emitted"
    TOOL_CALLED = "workflow.tool.called"
    POLICY_DECISION_MADE = "policy.decision.made"


def new_event_id() -> EventId:
    """Mint a fresh ``ev_<uuid>``. UUID4-backed for global uniqueness."""
    return EventId(f"ev_{uuid.uuid4()}")


# ---------------------------------------------------------------------------
# Base envelope.
# ---------------------------------------------------------------------------


class WorkflowEvent(BaseModel):
    """Common-field envelope every executor-emitted event extends.

    Master spec §11.3 — the five top-level fields below are universal;
    node-scoped subtypes hoist :attr:`invocation_id` + :attr:`node_name`
    to the top level (see :class:`NodeScopedWorkflowEvent`).

    ``payload`` is a free-shape dict; per-kind tightening lands as
    Phase 3.2+ runtimes mature. Caller-chain (for the 3 named kinds)
    lives INSIDE payload per the build doc.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(
        frozen=False, extra="forbid"
    )

    event_id: EventId = Field(default_factory=new_event_id)
    parent_event_id: EventId | None = None
    kind: WorkflowEventKind
    workflow_run_id: WorkflowRunId
    trace_id: TraceId
    # Master spec §11 lists ``ts`` as a common envelope field. The
    # repo-wide convention (Phase 1 ``schemas.Event.created_at``,
    # ``WorkflowRun.created_at``, ``MemoryItem.created_at``, session
    # entities) is a Pydantic ``datetime`` field serialised to
    # ISO-8601 by ``model_dump(mode="json")``. ``ts`` follows the
    # same convention so a JSONL consumer doesn't need a second
    # decoder for workflow events. The volatile-field canonicaliser
    # at :mod:`tests.golden.runs_test` collapses every ``ts`` to the
    # literal ``<TS>`` placeholder, matching the Phase 1 helper at
    # :mod:`tests.golden._helpers`.
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] = Field(default_factory=dict)


class NodeScopedWorkflowEvent(WorkflowEvent):
    """Adds ``invocation_id`` + ``node_name`` at the envelope top level.

    The 6 ``workflow.node.*`` kinds all carry these two fields so
    consumers (tail viewers, golden-trace diffs) can correlate node
    events without parsing ``payload``.
    """

    invocation_id: InvocationId
    node_name: str


# ---------------------------------------------------------------------------
# 13 typed kinds.
# ---------------------------------------------------------------------------


class WorkflowRunStartedEvent(WorkflowEvent):
    kind: Literal[WorkflowEventKind.RUN_STARTED] = WorkflowEventKind.RUN_STARTED


class WorkflowRunCompletedEvent(WorkflowEvent):
    kind: Literal[WorkflowEventKind.RUN_COMPLETED] = (
        WorkflowEventKind.RUN_COMPLETED
    )


class WorkflowRunFailedEvent(WorkflowEvent):
    kind: Literal[WorkflowEventKind.RUN_FAILED] = WorkflowEventKind.RUN_FAILED


class WorkflowRunTimedOutEvent(WorkflowEvent):
    kind: Literal[WorkflowEventKind.RUN_TIMED_OUT] = (
        WorkflowEventKind.RUN_TIMED_OUT
    )


class WorkflowRunAbortedEvent(WorkflowEvent):
    kind: Literal[WorkflowEventKind.RUN_ABORTED] = WorkflowEventKind.RUN_ABORTED


class WorkflowNodeStartedEvent(NodeScopedWorkflowEvent):
    kind: Literal[WorkflowEventKind.NODE_STARTED] = (
        WorkflowEventKind.NODE_STARTED
    )


class WorkflowNodeCompletedEvent(NodeScopedWorkflowEvent):
    kind: Literal[WorkflowEventKind.NODE_COMPLETED] = (
        WorkflowEventKind.NODE_COMPLETED
    )


class WorkflowNodeFailedEvent(NodeScopedWorkflowEvent):
    kind: Literal[WorkflowEventKind.NODE_FAILED] = WorkflowEventKind.NODE_FAILED


class WorkflowNodeTimedOutEvent(NodeScopedWorkflowEvent):
    kind: Literal[WorkflowEventKind.NODE_TIMED_OUT] = (
        WorkflowEventKind.NODE_TIMED_OUT
    )


class WorkflowNodePrunedEvent(NodeScopedWorkflowEvent):
    """``workflow.node.pruned`` — Phase 3.3 §11.1 extension.

    Emitted when a node is structurally suppressed because every
    inbound edge was suppressed by upstream branch dispatch (master
    spec §6.6 reverse-reachability cascade). The payload carries
    ``{node_name, reason: "branch_not_taken", upstream_source,
    branch_value}`` so JSONL forensics can correlate the prune back
    to the branchable source whose decision caused the cascade.

    Ordering rule per master spec §11.2 — pruned events follow the
    source's :class:`WorkflowNodeCompletedEvent` in the JSONL; when
    a single source prunes multiple descendants the events are
    contiguous. The ``parent_event_id`` chains to that source's
    completion id so consumers can walk the prune subtree without
    re-parsing the graph.

    The node-scoped envelope hoists ``invocation_id`` + ``node_name``
    to the top level for consistency with the rest of the
    ``workflow.node.*`` family; ``invocation_id`` is a fresh sentinel
    (``inv_pruned_<uuid>``) since a pruned node never produced a
    real invocation.
    """

    kind: Literal[WorkflowEventKind.NODE_PRUNED] = WorkflowEventKind.NODE_PRUNED


class WorkflowNodeLoggedEvent(NodeScopedWorkflowEvent):
    kind: Literal[WorkflowEventKind.NODE_LOGGED] = WorkflowEventKind.NODE_LOGGED


class WorkflowNodeEmittedEvent(NodeScopedWorkflowEvent):
    kind: Literal[WorkflowEventKind.NODE_EMITTED] = (
        WorkflowEventKind.NODE_EMITTED
    )


class WorkflowToolCalledEvent(NodeScopedWorkflowEvent):
    """``workflow.tool.called`` is node-scoped (a node fires the tool)
    AND carries ``caller_chain`` in payload (master spec §11.3).
    """

    kind: Literal[WorkflowEventKind.TOOL_CALLED] = WorkflowEventKind.TOOL_CALLED


class PolicyDecisionMadeEvent(WorkflowEvent):
    """``policy.decision.made`` is RUN-scoped (not node-scoped).

    Authz fires this from the rule walk inside the tool dispatcher;
    the dispatcher's enclosing node is identified by the
    ``parent_event_id`` chain back to a ``workflow.tool.called`` /
    ``workflow.node.started`` event, not a top-level field.
    """

    kind: Literal[WorkflowEventKind.POLICY_DECISION_MADE] = (
        WorkflowEventKind.POLICY_DECISION_MADE
    )


# ---------------------------------------------------------------------------
# Emit helper.
# ---------------------------------------------------------------------------


def emit_event(
    store: WorkflowRunStore,
    event: WorkflowEvent,
) -> EventId:
    """Append ``event`` to the run's JSONL via the store surface.

    Returns the envelope's ``event_id`` so the caller can stash it as
    ``parent_event_id`` on a downstream emit (master spec §11.2 +
    Phase 1 causal-parent convention).
    """
    store.append_event(event.workflow_run_id, event.model_dump(mode="json"))
    return event.event_id
