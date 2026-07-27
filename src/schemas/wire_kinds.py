"""Wire kind constants — canonical location for every ``kolsch.*`` UI event kind.

Every emit site and every dispatch site matches on these constants;
raw string literals are a spec violation.

Adding a new kind is a two-line change:

1. Add the constant to the appropriate section below.
2. Update the known-kinds table in the platform's event docs.

If a bundle wants a new kind in v1.1, it uses its own prefix (e.g.
``acme.table.rendered``). The platform reserves ``kolsch.*``.

See `process/spec-002-ui-event-schema.md` for the design contract.
"""

from __future__ import annotations

# --- Card kinds — emitted by CardsImpl (spec-006) ---------------------
WIRE_CARD_OPENED = "kolsch.card.opened"
WIRE_CARD_APPENDED = "kolsch.card.appended"
WIRE_CARD_SET = "kolsch.card.set"
WIRE_CARD_SET_PROGRESS = "kolsch.card.set_progress"
WIRE_CARD_STATUS = "kolsch.card.status"
WIRE_CARD_CLOSED = "kolsch.card.closed"
WIRE_CARD_SNAPSHOT = "kolsch.card.snapshot"
WIRE_CARD_NOTICE = "kolsch.card.notice"

# --- Prompt kinds — emitted by PromptsImpl (spec-004) -----------------
WIRE_PROMPT_REQUESTED = "kolsch.prompt.requested"
WIRE_PROMPT_ANSWERED = "kolsch.prompt.answered"
WIRE_PROMPT_CANCELLED = "kolsch.prompt.cancelled"

# --- Workflow UI kinds — emitted by WorkflowsImpl (spec-012+) ---------
# Note: these are DIFFERENT from the `WorkflowEventKind` values in
# `src/workflows/events.py`. Those are executor-owned domain events
# with their own `WorkflowEventKind` enum. These are UI translations
# that WorkflowsImpl emits AFTER receiving the corresponding domain
# events. spec-012 wires the translation path.
WIRE_WORKFLOW_PROGRESS = "kolsch.workflow.progress"

# spec-014: workflow-scoped approval lifecycle. Emitted durably at
# critical lane so `--resume` replays the paused/running transition.
WIRE_WORKFLOW_NODE_AWAITING_APPROVAL = "kolsch.workflow.node.awaiting_approval"
WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED = "kolsch.workflow.node.approval_resolved"

# spec-020: sub-agent event forwarder — tees the agent engine's per-
# event stream (from the child session spawned by a workflow's agent
# node) into UI events routed to the matching `WorkflowNodeRow` via
# bug-033's `(trace_id, invocation_id)` map. Envelope:
#     {run_id, node_name, invocation_id, trace_id,
#      event_type, event_id, payload}
# where `event_type` is the original session `Event.type` value and
# `payload` is the original `Event.data`.
WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT = "kolsch.workflow.node.sub_agent_event"


# --- Aggregate set — for validator + tests ----------------------------
ALL_PLATFORM_KINDS: frozenset[str] = frozenset(
    {
        WIRE_CARD_OPENED,
        WIRE_CARD_APPENDED,
        WIRE_CARD_SET,
        WIRE_CARD_SET_PROGRESS,
        WIRE_CARD_STATUS,
        WIRE_CARD_CLOSED,
        WIRE_CARD_SNAPSHOT,
        WIRE_CARD_NOTICE,
        WIRE_PROMPT_REQUESTED,
        WIRE_PROMPT_ANSWERED,
        WIRE_PROMPT_CANCELLED,
        WIRE_WORKFLOW_PROGRESS,
        WIRE_WORKFLOW_NODE_AWAITING_APPROVAL,
        WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED,
        WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT,
    }
)


_MIN_KIND_COMPONENTS = 3  # <owner>.<domain>.<verb>


def is_valid_kind(kind: str) -> bool:
    """Return True iff ``kind`` is a dotted ``<owner>.<domain>.<verb>``.

    Requires at least two dots (three components). Does NOT check
    membership in :data:`ALL_PLATFORM_KINDS` — bundle-authored kinds
    (v1.1) are legal at emit time even though they're not in the
    platform set. Membership checks happen at dispatch time.

    Each component must be a non-empty valid Python identifier
    (``str.isidentifier()``); this rejects empty components (from
    leading/trailing/double dots) and components starting with a
    digit.
    """
    parts = kind.split(".")
    if len(parts) < _MIN_KIND_COMPONENTS:
        return False
    return all(p and p.isidentifier() for p in parts)


__all__ = [
    "ALL_PLATFORM_KINDS",
    "WIRE_CARD_APPENDED",
    "WIRE_CARD_CLOSED",
    "WIRE_CARD_NOTICE",
    "WIRE_CARD_OPENED",
    "WIRE_CARD_SET",
    "WIRE_CARD_SET_PROGRESS",
    "WIRE_CARD_SNAPSHOT",
    "WIRE_CARD_STATUS",
    "WIRE_PROMPT_ANSWERED",
    "WIRE_PROMPT_CANCELLED",
    "WIRE_PROMPT_REQUESTED",
    "WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED",
    "WIRE_WORKFLOW_NODE_AWAITING_APPROVAL",
    "WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT",
    "WIRE_WORKFLOW_PROGRESS",
    "is_valid_kind",
]
