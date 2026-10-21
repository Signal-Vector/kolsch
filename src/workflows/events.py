"""Workflow event envelopes — typed JSONL payloads.

This module ships:

- [`WorkflowEventKind`][workflows.events.WorkflowEventKind] — the
  executor-emitted event vocabulary.
- [`WorkflowEvent`][workflows.events.WorkflowEvent] — common-field
  base envelope.
- [`NodeScopedWorkflowEvent`][workflows.events.NodeScopedWorkflowEvent]
  — adds `invocation_id` + `node_name` at the top level for the
  `workflow.node.*` kinds.
- One typed subclass per kind, each with `kind: Literal[...]` so
  Pydantic discriminates on emit. The subclasses are thin — payload
  remains a `dict[str, Any]` for most kinds; per-kind payload
  tightening lands as runtimes mature.
- `new_event_id` — mint a fresh `ev_<uuid>`.
- `emit_event` — append a typed envelope through the
  [`WorkflowRunStore`][workflows.run_store.WorkflowRunStore] JSONL
  surface.

The on-disk JSONL line is the envelope's `model_dump(mode="json")`
output: a single object with the common fields at the top level and
`payload` carrying kind-specific data.
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
    "WorkflowNodeCalledWorkflowCompletedEvent",
    "WorkflowNodeCalledWorkflowCompletedPayload",
    "WorkflowNodeCalledWorkflowEvent",
    "WorkflowNodeCalledWorkflowPayload",
    "WorkflowNodeCompletedEvent",
    "WorkflowNodeEmittedEvent",
    "WorkflowNodeFailedEvent",
    "WorkflowNodeLoggedEvent",
    "WorkflowNodePrunedEvent",
    "WorkflowNodeStartedEvent",
    "WorkflowNodeTimedOutEvent",
    "WorkflowRunAbortedEvent",
    "WorkflowRunAbortedPayload",
    "WorkflowRunCompletedEvent",
    "WorkflowRunCompletedPayload",
    "WorkflowRunFailedEvent",
    "WorkflowRunFailedPayload",
    "WorkflowRunForkedFromEvent",
    "WorkflowRunForkedFromPayload",
    "WorkflowRunResumedEvent",
    "WorkflowRunStartedEvent",
    "WorkflowRunStartedPayload",
    "WorkflowRunTimedOutEvent",
    "WorkflowStateMutatedEvent",
    "WorkflowStateMutatedPayload",
    "WorkflowToolCalledEvent",
    "emit_event",
    "new_event_id",
]


class WorkflowEventKind(StrEnum):
    """The executor-emitted event vocabulary.

    `model.request.sent` is the agent-emitted caller-chain carrier; it
    lives in the agent runtime, so this enum covers only the
    workflow-engine-owned kinds.
    """

    RUN_STARTED = "workflow.run.started"
    RUN_COMPLETED = "workflow.run.completed"
    RUN_FAILED = "workflow.run.failed"
    RUN_TIMED_OUT = "workflow.run.timed_out"
    RUN_ABORTED = "workflow.run.aborted"
    # First event emitted on every resume process; chained to the
    # JSONL's last pre-pause event via `parent_event_id` so the
    # cross-process event chain is unbroken.
    RUN_RESUMED = "workflow.run.resumed"
    NODE_STARTED = "workflow.node.started"
    NODE_COMPLETED = "workflow.node.completed"
    NODE_FAILED = "workflow.node.failed"
    NODE_TIMED_OUT = "workflow.node.timed_out"
    NODE_PRUNED = "workflow.node.pruned"
    NODE_LOGGED = "workflow.node.logged"
    NODE_EMITTED = "workflow.node.emitted"
    TOOL_CALLED = "workflow.tool.called"
    POLICY_DECISION_MADE = "policy.decision.made"
    # Emitted by the state-mutation runners (`set_state` /
    # `delete_state`) ONCE PER affected key, ordered BETWEEN the
    # node's `workflow.node.started` and `workflow.node.completed`
    # events. The JSONL sequence is the source of truth for resume
    # reconstruction and fork replay.
    STATE_MUTATED = "workflow.state.mutated"
    # Parent-side forensics markers around a `kolsch/nodes/workflow`
    # invocation. The pair brackets the child run's lifecycle in the
    # PARENT's JSONL: `CALLED_WORKFLOW` fires before the child run is
    # started (carries the child's `workflow_run_id` +
    # `workflow_ref`); `CALLED_WORKFLOW_COMPLETED` fires after the
    # child returns / pauses / fails (carries the child's
    # terminal-ish status). Together they let consumers navigate
    # parent→child JSONLs without re-deriving the linkage.
    CALLED_WORKFLOW = "workflow.node.called_workflow"
    CALLED_WORKFLOW_COMPLETED = "workflow.node.called_workflow_completed"
    # Emitted as the FIRST event of every forked run's JSONL (mirrors
    # `RUN_RESUMED`'s discipline: a new kind rather than a payload
    # extension on `RUN_STARTED` so event-stream consumers' switch
    # statements stay clean). The forked run shares the parent's
    # `trace_id`; the parent run id + fork event id live in
    # `WorkflowRunForkedFromPayload`.
    RUN_FORKED_FROM = "workflow.run.forked_from"


def new_event_id() -> EventId:
    """Mint a fresh `ev_<uuid>`. UUID4-backed for global uniqueness."""
    return EventId(f"ev_{uuid.uuid4()}")


# ---------------------------------------------------------------------------
# Base envelope.
# ---------------------------------------------------------------------------


class WorkflowEvent(BaseModel):
    """Common-field envelope every executor-emitted event extends.

    The top-level fields below are universal; node-scoped subtypes
    hoist `invocation_id` + `node_name` to the top level (see
    [`NodeScopedWorkflowEvent`][workflows.events.NodeScopedWorkflowEvent]).

    `payload` is a free-shape dict; per-kind tightening lands as
    runtimes mature. Caller-chain (for the relevant kinds) lives
    INSIDE payload.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=False, extra="forbid")

    event_id: EventId = Field(default_factory=new_event_id)
    parent_event_id: EventId | None = None
    kind: WorkflowEventKind
    workflow_run_id: WorkflowRunId
    trace_id: TraceId
    # `ts` is the common envelope timestamp. The repo-wide convention
    # (`schemas.Event.created_at`, `WorkflowRun.created_at`,
    # `MemoryItem.created_at`, session entities) is a Pydantic
    # `datetime` field serialised to ISO-8601 by
    # `model_dump(mode="json")`. `ts` follows the same convention so a
    # JSONL consumer doesn't need a second decoder for workflow
    # events. The volatile-field canonicaliser in the golden tests
    # collapses every `ts` to the literal `<TS>` placeholder.
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] = Field(default_factory=dict)


class NodeScopedWorkflowEvent(WorkflowEvent):
    """Adds `invocation_id` + `node_name` at the envelope top level.

    The `workflow.node.*` kinds all carry these two fields so
    consumers (tail viewers, golden-trace diffs) can correlate node
    events without parsing `payload`.
    """

    invocation_id: InvocationId
    node_name: str


# ---------------------------------------------------------------------------
# 13 typed kinds.
# ---------------------------------------------------------------------------


class WorkflowRunStartedPayload(BaseModel):
    """Typed payload for the `workflow.run.started` envelope.

    Anchors the `concurrency_mode` field. `extra="allow"` so existing
    payload keys (`workflow_ref`, `inputs`, `trace_id`) round-trip
    cleanly when loaded from older JSONL rows; `concurrency_mode`
    defaults to `None` for the same reason.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    concurrency_mode: Literal["sequential", "auto"] | None = None


class WorkflowRunStartedEvent(WorkflowEvent):
    """The `workflow.run.started` envelope."""

    kind: Literal[WorkflowEventKind.RUN_STARTED] = WorkflowEventKind.RUN_STARTED


class WorkflowRunCompletedPayload(BaseModel):
    """Typed payload for the `workflow.run.completed` envelope.

    Anchors the `peak_in_flight` field — the maximum number of
    concurrent in-flight node invocations observed during the run, a
    forensics aid for authors tuning their workflows. `extra="allow"`
    so existing payload keys (`outputs`) round-trip cleanly when
    loaded from older JSONL rows; `peak_in_flight` defaults to `None`
    for the same reason.

    Also anchors `terminal_at`: the UTC datetime at payload
    construction. spec-016's multi-workflow layout sorts terminal
    cards by this field; putting it in place from M1 onward guarantees
    every completed run on disk carries it. Default-factory runs at
    model_validate, so older logs pick up a sensible value on read.
    Emitters that need precise emit-time semantics should override
    explicitly.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    peak_in_flight: int | None = None
    terminal_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class WorkflowRunCompletedEvent(WorkflowEvent):
    """The `workflow.run.completed` envelope."""

    kind: Literal[WorkflowEventKind.RUN_COMPLETED] = WorkflowEventKind.RUN_COMPLETED


class WorkflowRunFailedPayload(BaseModel):
    """Typed payload for the `workflow.run.failed` envelope.

    Anchors `terminal_at`: the UTC datetime at payload construction.
    See :class:`WorkflowRunCompletedPayload` for the rationale
    (spec-016 sorts terminal cards by this field). Payload otherwise
    stays `extra="allow"` so existing failure keys (`error`,
    `failure_reason`, etc.) round-trip cleanly through older JSONL
    rows.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    terminal_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class WorkflowRunFailedEvent(WorkflowEvent):
    """The `workflow.run.failed` envelope."""

    kind: Literal[WorkflowEventKind.RUN_FAILED] = WorkflowEventKind.RUN_FAILED


class WorkflowRunTimedOutEvent(WorkflowEvent):
    """The `workflow.run.timed_out` envelope."""

    kind: Literal[WorkflowEventKind.RUN_TIMED_OUT] = WorkflowEventKind.RUN_TIMED_OUT


class WorkflowRunAbortedPayload(BaseModel):
    """Typed payload for the `workflow.run.aborted` envelope.

    Anchors `terminal_at`: the UTC datetime at payload construction.
    See :class:`WorkflowRunCompletedPayload` for the rationale
    (spec-016 sorts terminal cards by this field). Payload otherwise
    stays `extra="allow"` so existing abort keys (`reason`, etc.)
    round-trip cleanly through older JSONL rows.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    terminal_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class WorkflowRunAbortedEvent(WorkflowEvent):
    """The `workflow.run.aborted` envelope."""

    kind: Literal[WorkflowEventKind.RUN_ABORTED] = WorkflowEventKind.RUN_ABORTED


class WorkflowRunResumedEvent(WorkflowEvent):
    """The `workflow.run.resumed` envelope.

    Emitted as the FIRST event of every resume process, BEFORE the
    paused `user_input` node's completion event. `parent_event_id`
    chains to the last event in the JSONL at pause time (typically the
    paused user_input node's
    [`WorkflowNodeStartedEvent`][workflows.events.WorkflowNodeStartedEvent])
    so the cross-process event chain is unbroken.

    Payload carries `resumed_at_invocation_id` so forensics readers
    can correlate the resume directly to the open invocation it
    completes, without walking the JSONL backwards. The chat capsule
    consumes this event to visually mark resumed runs.
    """

    kind: Literal[WorkflowEventKind.RUN_RESUMED] = WorkflowEventKind.RUN_RESUMED


class WorkflowRunForkedFromPayload(BaseModel):
    """Payload for the `workflow.run.forked_from` envelope.

    Frozen + `extra="forbid"` so a typo'd payload field is a
    construction-time error rather than a silently-dropped JSONL line.
    `input_overrides` carries the `--input k=v` pairs the operator
    passed to `kolsch workflows fork`; an empty dict means no
    overrides were applied at the fork point.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    forked_from_run_id: str
    fork_event_id: str
    input_overrides: dict[str, Any] = Field(default_factory=dict)


class WorkflowRunForkedFromEvent(WorkflowEvent):
    """The `workflow.run.forked_from` envelope.

    Emitted as the FIRST event of every forked run's JSONL (before
    any replay-driven completion events the executor synthesizes on
    resume-from-fork). Mirrors
    [`WorkflowRunResumedEvent`][workflows.events.WorkflowRunResumedEvent]'s
    discipline — a new kind rather than a payload extension on
    `RUN_STARTED` so event-stream consumers can switch on `kind`
    without inspecting payload shape. `parent_event_id` is `None` on
    this envelope (it IS the JSONL's first event); the cross-JSONL
    link to the parent run is the payload's `forked_from_run_id` +
    `fork_event_id` pair plus the inherited `trace_id`.
    """

    kind: Literal[WorkflowEventKind.RUN_FORKED_FROM] = WorkflowEventKind.RUN_FORKED_FROM
    # Narrower payload than the base's `dict[str, Any]`. Pydantic
    # accepts the override; mypy's strict mode reads it as invariant.
    payload: WorkflowRunForkedFromPayload  # type: ignore[assignment]


class WorkflowNodeStartedEvent(NodeScopedWorkflowEvent):
    """The `workflow.node.started` envelope."""

    kind: Literal[WorkflowEventKind.NODE_STARTED] = WorkflowEventKind.NODE_STARTED


class WorkflowNodeCompletedEvent(NodeScopedWorkflowEvent):
    """The `workflow.node.completed` envelope.

    The payload carries `{node_name, invocation_id, output}` plus
    `branch_value` (`bool | str`) ONLY when the node is used as a
    branch source (at least one outgoing edge has `branch:`); a str
    value is a `kolsch/nodes/jev` choice (spec-136).
    """

    kind: Literal[WorkflowEventKind.NODE_COMPLETED] = WorkflowEventKind.NODE_COMPLETED


class WorkflowNodeFailedEvent(NodeScopedWorkflowEvent):
    """The `workflow.node.failed` envelope."""

    kind: Literal[WorkflowEventKind.NODE_FAILED] = WorkflowEventKind.NODE_FAILED


class WorkflowNodeTimedOutEvent(NodeScopedWorkflowEvent):
    """The `workflow.node.timed_out` envelope."""

    kind: Literal[WorkflowEventKind.NODE_TIMED_OUT] = WorkflowEventKind.NODE_TIMED_OUT


class WorkflowNodePrunedEvent(NodeScopedWorkflowEvent):
    """The `workflow.node.pruned` envelope.

    Emitted when a node is structurally suppressed because every
    inbound edge was suppressed by upstream branch dispatch (the
    reverse-reachability cascade). The payload carries
    `{node_name, reason: "branch_not_taken", upstream_source,
    branch_value}` so JSONL forensics can correlate the prune back to
    the branchable source whose decision caused the cascade.
    `branch_value` is `bool | str`: a bool for boolean sources, the
    chosen option name (str) for a `kolsch/nodes/jev` choice
    (spec-136 multi-way branching).

    Pruned events follow the source's
    [`WorkflowNodeCompletedEvent`][workflows.events.WorkflowNodeCompletedEvent]
    in the JSONL; when a single source prunes multiple descendants
    the events are contiguous. The `parent_event_id` chains to that
    source's completion id so consumers can walk the prune subtree
    without re-parsing the graph.

    The node-scoped envelope hoists `invocation_id` + `node_name` to
    the top level for consistency with the rest of the
    `workflow.node.*` family; `invocation_id` is a fresh sentinel
    (`inv_pruned_<uuid>`) since a pruned node never produced a real
    invocation.
    """

    kind: Literal[WorkflowEventKind.NODE_PRUNED] = WorkflowEventKind.NODE_PRUNED


class WorkflowNodeLoggedEvent(NodeScopedWorkflowEvent):
    """The `workflow.node.logged` envelope."""

    kind: Literal[WorkflowEventKind.NODE_LOGGED] = WorkflowEventKind.NODE_LOGGED


class WorkflowNodeEmittedEvent(NodeScopedWorkflowEvent):
    """The `workflow.node.emitted` envelope."""

    kind: Literal[WorkflowEventKind.NODE_EMITTED] = WorkflowEventKind.NODE_EMITTED


class WorkflowToolCalledEvent(NodeScopedWorkflowEvent):
    """The `workflow.tool.called` envelope.

    Node-scoped (a node fires the tool) AND carries `caller_chain` in
    payload.
    """

    kind: Literal[WorkflowEventKind.TOOL_CALLED] = WorkflowEventKind.TOOL_CALLED


class WorkflowStateMutatedPayload(BaseModel):
    """Payload for the `workflow.state.mutated` envelope.

    Frozen + `extra="forbid"` so a typo'd payload field is a
    construction-time error rather than a silently-dropped JSONL
    line. `value` is `None` for `"delete"` ops and the rendered Jinja
    result (or YAML literal) for `"set"` ops; the executor's state
    dict is typed `Any` so no further narrowing applies here.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    key: str
    op: Literal["set", "delete"]
    value: Any | None = None


class WorkflowStateMutatedEvent(WorkflowEvent):
    """The `workflow.state.mutated` envelope.

    NOT node-scoped at the envelope top level: state is treated as a
    workflow-run-scoped concern (mutations belong to the run's state
    history, not specifically to the emitting node's invocation). The
    emitting node is still recoverable via the `parent_event_id`
    chain back to that node's `workflow.node.started` event.
    """

    kind: Literal[WorkflowEventKind.STATE_MUTATED] = WorkflowEventKind.STATE_MUTATED
    payload: WorkflowStateMutatedPayload  # type: ignore[assignment]  # narrower payload; see WorkflowRunForkedFromEvent


class WorkflowNodeCalledWorkflowPayload(BaseModel):
    """Payload for the `workflow.node.called_workflow` envelope.

    Frozen + `extra="forbid"` so a typo'd payload field is a
    construction-time error rather than a silently-dropped JSONL
    line. Emitted by the parent's executor BEFORE the workflow node's
    runner returns, after the child run has been minted.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    node_name: str
    invocation_id: str
    child_workflow_run_id: str
    child_workflow_ref: str


class WorkflowNodeCalledWorkflowEvent(NodeScopedWorkflowEvent):
    """The `workflow.node.called_workflow` envelope.

    Parent-side forensics marker emitted between the workflow node's
    own `workflow.node.started` and `workflow.node.completed`.
    Carries the child run's identifying fields so cross-JSONL
    navigation works without re-deriving the parent→child linkage.
    """

    kind: Literal[WorkflowEventKind.CALLED_WORKFLOW] = WorkflowEventKind.CALLED_WORKFLOW
    payload: WorkflowNodeCalledWorkflowPayload  # type: ignore[assignment]  # narrower payload; see WorkflowRunForkedFromEvent


class WorkflowNodeCalledWorkflowCompletedPayload(BaseModel):
    """Payload for the `workflow.node.called_workflow_completed` envelope.

    `child_status` is the child run's terminal-ish status string:
    `"succeeded"` / `"failed"` / `"waiting_for_input"` / `"aborted"`
    / `"timed_out"` (the
    [`WorkflowRunStatus`][workflows.run.WorkflowRunStatus] lowercased
    values).
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    node_name: str
    invocation_id: str
    child_workflow_run_id: str
    child_status: str


class WorkflowNodeCalledWorkflowCompletedEvent(NodeScopedWorkflowEvent):
    """The `workflow.node.called_workflow_completed` envelope.

    Parent-side bookend to
    [`WorkflowNodeCalledWorkflowEvent`][workflows.events.WorkflowNodeCalledWorkflowEvent].
    Emitted by the workflow runner AFTER the child run reaches a
    terminal-ish status (succeeded / failed / waiting_for_input /
    aborted / timed_out). For pause cascade, the parent emits this
    with `child_status="waiting_for_input"` then re-raises
    [`WorkflowPausedForInput`][workflows.errors.WorkflowPausedForInput]
    so the parent's executor catches it and pauses.
    """

    kind: Literal[WorkflowEventKind.CALLED_WORKFLOW_COMPLETED] = (
        WorkflowEventKind.CALLED_WORKFLOW_COMPLETED
    )
    payload: WorkflowNodeCalledWorkflowCompletedPayload  # type: ignore[assignment]  # narrower payload; see WorkflowRunForkedFromEvent


class PolicyDecisionMadeEvent(WorkflowEvent):
    """The `policy.decision.made` envelope — RUN-scoped, not node-scoped.

    Authz fires this from the rule walk inside the tool dispatcher;
    the dispatcher's enclosing node is identified by the
    `parent_event_id` chain back to a `workflow.tool.called` /
    `workflow.node.started` event, not a top-level field.
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
    """Append `event` to the run's JSONL via the store surface.

    Args:
        store: The run store to append through.
        event: The typed event envelope.

    Returns:
        The envelope's `event_id` — caller can stash it as
        `parent_event_id` on a downstream emit.
    """
    store.append_event(event.workflow_run_id, event.model_dump(mode="json"))
    return event.event_id
