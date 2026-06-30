"""Typed prefixed IDs for the workflow runtime.

Four [`PrefixedId`][schemas.identifiers.PrefixedId] subclasses, each
with UUID4 bodies. Each ID has a single owner — the layer responsible
for minting it. Mis-owning an ID (e.g. chat code generating a
WorkflowRunId) breaks the contract that cross-JSONL forensics tools
rely on.

- `tr_` ([`TraceId`][workflows.ids.TraceId]) — owned by CLI / chat
  at the root entry point.
- `wrk_` ([`WorkflowRunId`][workflows.ids.WorkflowRunId]) — owned by
  the workflow engine.
- `inv_` ([`InvocationId`][workflows.ids.InvocationId]) — owned by
  the workflow engine.
- `tc_` ([`ToolCallId`][workflows.ids.ToolCallId]) — owned by the
  tool dispatcher.

Every event id the workflow engine, agent engine, or tool dispatcher
emits is drawn from UUID4 so cross-layer concatenations never collide.
"""

from __future__ import annotations

from schemas.identifiers import PrefixedId

__all__ = [
    "InvocationId",
    "ToolCallId",
    "TraceId",
    "WorkflowRunId",
]


class TraceId(PrefixedId):
    """`tr_<uuid>` — the chain-spanning trace id.

    CLI / chat assigns at the root entry point. Inherited unchanged
    across sub-workflow calls, spawned agent sessions, sub-agent
    invocations, and forks so a single trace string follows the work
    across layered emissions.
    """

    prefix = "tr_"
    body_must_be_uuid = True


class WorkflowRunId(PrefixedId):
    """`wrk_<uuid>` — one workflow run.

    Owned by the workflow engine. The unit of cancellation, fork, and
    event-log retention.
    """

    prefix = "wrk_"
    body_must_be_uuid = True


class InvocationId(PrefixedId):
    """`inv_<uuid>` — one node invocation.

    Owned by the workflow engine. Distinct per runtime fire of a node
    — `nodes.<name>` is the workflow-level addressable handle on the
    captured output of a single invocation site.
    """

    prefix = "inv_"
    body_must_be_uuid = True


class ToolCallId(PrefixedId):
    """`tc_<uuid>` — one tool dispatch.

    Owned by the tool dispatcher. Typed (rather than a bare `str`) so
    observers and the JSONL serialiser pass it through without losing
    identity at module boundaries.
    """

    prefix = "tc_"
    body_must_be_uuid = True


# Break the schemas ↔ workflows.ids import cycle.
#
# `schemas.entities` declares `ParentRunPointer` / `FromWorkflowRun`
# with string forward refs to `WorkflowRunId` / `InvocationId` defined
# above. The Pydantic models need the names bound for validation; the
# cycle prevents top-level resolution at schemas.entities load time
# (workflows.ids is mid-init then). Now that workflows.ids is FULLY
# loaded, ask schemas.entities to rebind its forward refs. The runtime
# `import` here is safe — by the time Python reaches this line,
# schemas.entities is also fully loaded (its initial import returned
# earlier in the same call chain).
try:
    import schemas.entities as _schemas_entities

    if hasattr(_schemas_entities, "_resolve_forward_refs"):
        _schemas_entities._resolve_forward_refs()
except ImportError:
    # The very first load that triggered this cycle may not have
    # schemas.entities available yet (Python's import lock + partial
    # init). The next consumer that imports schemas.entities directly
    # will trigger its own `_resolve_forward_refs` at its module
    # bottom successfully.
    pass
