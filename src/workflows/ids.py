"""Typed prefixed IDs for the workflow runtime — master spec §11 (R3 #10).

Four prefixed-UUID identifier types live here, each one a thin
:class:`schemas.identifiers.PrefixedId` subclass. UUID4 backing
guarantees global uniqueness for ``EventId``-paired IDs (see
:class:`schemas.identifiers.EventId`).

Layered ID ownership map (master spec §11.x):

- ``tr_`` (:class:`TraceId`) — CLI/chat owns assignment at the root
  entry point. Inherited UNCHANGED across sub-workflow calls,
  spawned agent sessions, sub-agent invocations, and forks. The
  chain-spanning ID.
- ``wrk_`` (:class:`WorkflowRunId`) — workflow engine owns
  assignment. One per workflow run.
- ``inv_`` (:class:`InvocationId`) — workflow engine owns
  assignment. One per invocation (the runtime fire of a node;
  master spec §11 Glossary).
- ``tc_`` (:class:`ToolCallId`) — tool dispatcher owns assignment.
  Was a bare ``str`` pre-R3 #10; now a typed class so the
  observers + JSONL serializer can pass it through without losing
  identity at type boundaries.
- ``ses_``/``th_``/``cl_`` — owned by the agent runtime
  (:mod:`schemas.identifiers`).
- ``ev_`` — owned by the event emitter
  (:class:`schemas.identifiers.EventId`).

UUID4 invariant for ``EventId`` global uniqueness: every event id
the workflow engine, agent runtime, or tool dispatcher emits must
be drawn from UUID4 so cross-layer concatenations never collide.
The IDs in this module follow the same convention.
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
    """``tr_<uuid>`` — the chain-spanning trace ID (R3 #10).

    CLI/chat assigns at the root entry point. Inherited unchanged
    across sub-workflow calls, spawned agent sessions, sub-agent
    invocations, and forks so a single trace string follows the
    work across layered emissions.
    """

    prefix = "tr_"
    body_must_be_uuid = True


class WorkflowRunId(PrefixedId):
    """``wrk_<uuid>`` — one workflow run (R3 #10).

    Workflow engine owns assignment. The unit of cancellation,
    fork, and event-log retention.
    """

    prefix = "wrk_"
    body_must_be_uuid = True


class InvocationId(PrefixedId):
    """``inv_<uuid>`` — one node invocation (R3 #10).

    Workflow engine owns assignment. Distinct per runtime fire of a
    node — `nodes.<name>` is the workflow-level addressable handle
    on the captured output of a single invocation site.
    """

    prefix = "inv_"
    body_must_be_uuid = True


class ToolCallId(PrefixedId):
    """``tc_<uuid>`` — one tool dispatch (R3 #10).

    Tool dispatcher owns assignment. Pre-R3 #10 this was a bare
    ``str`` (see :attr:`tools.decorator.ToolContext.tool_call_id`);
    typing it lifts the discipline at module boundaries.
    """

    prefix = "tc_"
    body_must_be_uuid = True
