"""Event envelope + per-type data models — Schemas §B.2-B.8.

Event types are organised as a :class:`StrEnum` (``EventType``) so
emitters get autocomplete + Pydantic validation and so consumers can
exhaustively switch over the set.

Each event type has a dedicated ``*Data`` Pydantic model declaring its
``data`` payload per Schemas §B.8. The :class:`Event` envelope holds
``data`` as a permissive ``dict[str, Any]`` so the on-disk JSONL stays
forward-compatible; callers that want strict validation construct the
matching ``*Data`` model and pass ``.model_dump()`` into the envelope.

Conflict (flagged): Schemas §B.2 notes ``session_id`` is NOT in the
envelope ("the file IS the session"). Module API Reference §13.2
includes ``session_id`` as a field. The Module API Reference is the
arbiter for runtime API shape, so :class:`Event` carries
``session_id``; serialisers writing the JSONL on disk are responsible
for stripping/re-adding it as the file format dictates.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from schemas.entities import Actor, ContentRef
from schemas.error import Error
from schemas.identifiers import (
    AgentCallId,
    AgentId,
    EventId,
    MemoryItemId,
    SessionId,
    ThreadId,
)


class EventType(StrEnum):
    """The Phase 1 event types — Schemas §B.7."""

    # Session identity (the file's first event)
    SESSION_OPENED = "session.opened"

    # Thread progress
    THREAD_EXCHANGE_STARTED = "thread.exchange_started"
    THREAD_EXCHANGE_COMPLETED = "thread.exchange_completed"
    THREAD_EXCHANGE_FAILED = "thread.exchange_failed"

    # Conversation content
    USER_MESSAGE = "user.message"
    AGENT_MESSAGE = "agent.message"
    TOOL_CALLED = "tool.called"
    TOOL_RESULT = "tool.result"
    TOOL_FAILED = "tool.failed"

    # Model interaction
    MODEL_REQUEST = "model.request"
    MODEL_RESPONSE = "model.response"
    # Phase 1.3 (FB-R4) — emitted once per fallback transition.
    MODEL_FALLBACK = "model.fallback"

    # Config
    CONFIG_SET = "config.set"

    # Memory
    MEMORY_WRITTEN = "memory.written"
    MEMORY_RETRIEVED = "memory.retrieved"
    # NOTE: spec silent on memory.deleted in §B.7, but Module API
    # Reference §5 has MemoryContext.delete with "engine separately
    # emits memory.deleted event". Included here so deletions are
    # captured in the log.
    MEMORY_DELETED = "memory.deleted"

    # Compaction
    CONTEXT_COMPACTED = "context.compacted"

    # Phase 2.2 (ENV §9) — template-config consistency warnings
    # emitted by the engine's pre-flight step 5.6.6 AST walker when a
    # prompt references variable data the resolved config doesn't carry
    # (e.g. ``{{ tools }}`` referenced + no tools declared).
    AGENT_TEMPLATE_WARNING = "agent.template_warning"


# ---------------------------------------------------------------------------
# Per-event-type `data` payload models — Schemas §B.8
# ---------------------------------------------------------------------------


class _DataModel(BaseModel):
    """Common base for event-data payloads.

    ``extra="forbid"`` enforces the catalog shape (E-I7-style for events):
    a typo in a field name fails validation rather than silently passing
    through into the JSONL.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class SessionOpenedData(_DataModel):
    """`session.opened.data` — §B.8."""

    session_id: SessionId
    initial_agent: dict[str, str]
    forked_from: dict[str, Any] | None = None


class ThreadExchangeStartedData(_DataModel):
    """`thread.exchange_started.data` — §B.8."""

    trigger_event_id: EventId


class ThreadExchangeCompletedData(_DataModel):
    """`thread.exchange_completed.data` — §B.8."""

    exchange_started_id: EventId
    agent_message_id: EventId


class ThreadExchangeFailedData(_DataModel):
    """`thread.exchange_failed.data` — §B.8 (Error Model §5.2)."""

    exchange_started_id: EventId
    error: Error


class UserMessageData(_DataModel):
    """`user.message.data` — §B.8.

    ``content`` is typed per ``IOSpec.inputs`` (conversational mode),
    captured here as a permissive dict so the envelope is reusable
    across agents with differing schemas.
    """

    content: dict[str, Any]
    images: list[dict[str, Any]] | None = None


class AgentMessageData(_DataModel):
    """`agent.message.data` — §B.8."""

    content: dict[str, Any]
    exchange_id: EventId


class ModelRequestData(_DataModel):
    """`model.request.data` — §B.8 + §B.6 draft-0.6 amendment.

    Schemas spec extended (2026-05-28) so the LLM-seam message list
    is captured verbatim on the request event. Before this amendment,
    §B.8 said "context is derivable from the tree via Assembly
    Procedure"; under a wrapping framework (CrewAI's prompt-template
    injection, multimodal reshaping) that derivation diverges from
    what the LLM actually saw. Capturing here closes the gap.

    Replay reads ``messages`` directly from this event for
    "what did the model see at this call". The Assembly Procedure
    still applies for projecting future calls (which haven't happened
    yet and therefore haven't been captured).

    Bug-engine-tools-wiring-gap.md Step F extends the capture to the
    tool list the LLM saw — Engine I3 (Materialization Rule) wants
    every input that contributed to the model's context recorded on
    the event, and the tool schemas DO contribute (the model uses
    them to decide tool_use). Before this extension the JSONL was
    silent on whether the model saw any tools at all — the symptom
    the user's hallucination bug surfaced.
    """

    call_id: AgentCallId
    model: dict[str, Any]
    config_event_id: EventId
    # §B.6 amendment — the LLM-seam request.messages dump. Captured
    # post-framework-wrap, pre-LLM-invoke. Replay sources from here,
    # not from the path.
    messages: list[dict[str, Any]] = Field(default_factory=list)
    # Bug-engine-tools-wiring-gap.md Step F — the LLM-seam tool
    # schema list. Empty when the agent declares no tools; populated
    # with one entry per registered tool (``{name, schema, ...}``)
    # otherwise. Engine I3 (Materialization Rule) compliance.
    tools: list[dict[str, Any]] = Field(default_factory=list)


class ModelResponseData(_DataModel):
    """`model.response.data` — §B.8 + §5.3 (error variant)."""

    call_id: AgentCallId
    content: dict[str, Any]
    reasoning_trace: dict[str, Any] | None = None
    usage: dict[str, Any]
    cost: float
    latency_ms: int
    stop_reason: str
    # When stop_reason == "error", `error` carries the canonical
    # Error envelope (Error Model §5.3). Optional otherwise.
    error: Error | None = None


class ModelFallbackData(_DataModel):
    """`model.fallback.data` — Phase 1.3 patch §3.1.

    Emitted by the engine once per fallback transition. Lives
    between the failing attempt (its ``model.request`` or
    ``model.response``) and the next attempt's ``model.request``
    per patch §3.2.

    - ``from`` / ``to`` carry canonical ``{provider, model}``
      dicts (the resolver normalises aliases at Layer 1).
    - ``reason`` is the failing attempt's error code (e.g.
      ``model.rate_limited``).
    - ``reason_detail`` is a one-line human-readable summary.
    - ``attempt`` is 1-indexed; attempt 0 was the primary, so
      the first ``model.fallback`` event always has
      ``attempt: 1``.
    """

    # `from` shadows the Python keyword; use field-aliased form so
    # the JSON shape (and the on-disk JSONL) matches patch §3.1
    # exactly. ``populate_by_name`` lets engine code construct via
    # the Python identifier (``from_model=``) while
    # ``model_dump(by_alias=True)`` round-trips to the spec'd JSON.
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    from_model: dict[str, str] = Field(alias="from")
    to_model: dict[str, str] = Field(alias="to")
    reason: str
    reason_detail: str = ""
    attempt: int


class ToolCalledData(_DataModel):
    """`tool.called.data` — §B.8."""

    tool_name: str
    tool_call_id: str
    arguments: dict[str, Any]


class ToolResultData(_DataModel):
    """`tool.result.data` — §B.8."""

    tool_call_id: str
    tool_name: str
    result: dict[str, Any]
    is_error: bool = False


class ToolFailedData(_DataModel):
    """`tool.failed.data` — §B.8 (Error Model §5.1).

    qol-permission-system-human-readable.md Step 3 — adds the
    optional ``recovery_hint`` field. When the tool failed because
    the authz provider denied it (``error.type == "authz.denied"``
    OR the wrapped ``tool.unauthorized`` whose detail names a deny
    rule), the engine populates this with the exact ``/perm allow
    …`` chat command the user can type to un-block. The chat
    renderer surfaces it inside the denied capsule; the framework
    forwards the whole event payload to the model so the agent
    sees the actionable text and stops flailing.

    ``None`` for every non-authz failure — the field is purely a
    convenience layer for the authz path; other tool failures
    continue to surface through ``error.message`` alone.
    """

    tool_call_id: str
    tool_name: str
    error: Error
    recovery_hint: str | None = None


class ConfigSetData(_DataModel):
    """`config.set.data` — §B.8.

    ``resolved_config`` is the FULL, fully-dereferenced
    ResolvedAgentConfig; captured here as a permissive dict so this
    module has no import dependency on :mod:`schemas.config`. Callers
    that want the typed model use :class:`ResolvedAgentConfig`
    directly.
    """

    resolved_config: dict[str, Any] = Field(min_length=1)
    config_hash: str


class MemoryWrittenData(_DataModel):
    """`memory.written.data` — §B.8 + extension.

    Spec §B.8 lists only ``scope / content / variant``; that is too
    sparse to satisfy MEM I3 (Memory's cache MUST be reconstructable
    from the log). The extension below carries the full MemoryItem
    record so a replay deterministically rebuilds the in-memory cache
    (MEM I9 — deterministic replay). Flag for spec amendment of §B.8.
    """

    item_id: MemoryItemId
    agent_id: AgentId
    scope: str
    content: dict[str, Any]
    tags: list[str]
    metadata: dict[str, Any] = Field(default_factory=dict)
    variant: str


class MemoryRetrievedData(_DataModel):
    """`memory.retrieved.data` — §B.8.

    Materialisation Rule (§B.6): ``results`` carries the actual items
    returned, verbatim. The Assembly Procedure uses this content; it
    never re-runs the query at replay time.
    """

    scope: str
    query: dict[str, Any]
    results: list[dict[str, Any]]
    variant: str


class MemoryDeletedData(_DataModel):
    """`memory.deleted.data` — spec silent on shape; inferred from
    Module API Reference §5 (MemoryContext.delete takes an item_id).
    """

    item_id: MemoryItemId


class ContextCompactedData(_DataModel):
    """`context.compacted.data` — §B.8.

    The log keeps everything; only the model's projected context is
    shorter. ``pre_compaction`` is the FULL content being summarised.
    """

    compacted_event_ids: list[EventId]
    pre_compaction: dict[str, Any]
    summary: dict[str, Any]
    tokens_before: int
    tokens_after: int


class AgentTemplateWarningData(_DataModel):
    """``agent.template_warning.data`` — Phase 2.2 §9 + ENV-R6 §4.18.

    Emitted by the engine's pre-flight step 5.6.6 when a prompt
    references variable data the resolved config doesn't carry.
    """

    reference: str
    reason: str
    hint: str


# ---------------------------------------------------------------------------
# The Event envelope — §B.2 + §13.2
# ---------------------------------------------------------------------------


class Event(BaseModel):
    """The canonical Event envelope — Module API Reference §13.2.

    ``data`` is intentionally loose (a dict). Callers that want strict
    per-type validation construct the matching ``*Data`` model and
    assign ``.model_dump()`` into ``data``; the engine and store
    consume the envelope shape uniformly.
    """

    model_config = ConfigDict(frozen=False, extra="forbid")

    event_id: EventId
    parent_event_id: EventId | None = None
    thread_id: ThreadId
    session_id: SessionId
    type: EventType
    data: dict[str, Any] = Field(default_factory=dict)
    actor: Actor
    created_at: datetime
    content_ref: ContentRef | None = None
