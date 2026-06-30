"""Event envelope plus the per-type data payload models.

Event types are organised as a [`StrEnum`][schemas.events.EventType] so
emitters get autocomplete plus Pydantic validation, and consumers can
exhaustively switch over the set.

Each event type has a dedicated `*Data` Pydantic model declaring its
payload shape. The [`Event`][schemas.events.Event] envelope holds
`data` as a permissive `dict[str, Any]` so the on-disk JSONL stays
forward-compatible; callers that want strict validation construct the
matching `*Data` model and pass `.model_dump()` into the envelope.

For the full event-log contract — which records belong to which
subsystem, ordering rules, and what consumers may assume — see the
[module API reference](../../docs/reference/module-api-reference.md).
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
    """Every event type the platform writes to the JSONL log.

    Grouped by subsystem in the declaration order below. New event
    types are added here first; emitters and consumers then branch on
    the value.
    """

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
    # Emitted once per fallback transition.
    MODEL_FALLBACK = "model.fallback"

    # Config
    CONFIG_SET = "config.set"

    # Memory
    MEMORY_WRITTEN = "memory.written"
    MEMORY_RETRIEVED = "memory.retrieved"
    # Captured so deletions surface in the log and the in-memory cache
    # reconstruction at replay time stays deterministic.
    MEMORY_DELETED = "memory.deleted"

    # Compaction
    CONTEXT_COMPACTED = "context.compacted"

    # Engine pre-flight emits one of these when a template references
    # variable data the resolved config doesn't carry (e.g.
    # `{{ tools }}` referenced but no tools declared).
    AGENT_TEMPLATE_WARNING = "agent.template_warning"


# ---------------------------------------------------------------------------
# Per-event-type `data` payload models
# ---------------------------------------------------------------------------


class _DataModel(BaseModel):
    """Common base for event-data payloads.

    `extra="forbid"` enforces the per-type shape: a typo in a field
    name fails validation rather than silently passing through into
    the JSONL.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class SessionOpenedData(_DataModel):
    """`session.opened.data` — payload for the session-identity event."""

    session_id: SessionId
    initial_agent: dict[str, str]
    forked_from: dict[str, Any] | None = None


class ThreadExchangeStartedData(_DataModel):
    """`thread.exchange_started.data` — opens one exchange on the thread."""

    trigger_event_id: EventId


class ThreadExchangeCompletedData(_DataModel):
    """`thread.exchange_completed.data` — closes one exchange successfully."""

    exchange_started_id: EventId
    agent_message_id: EventId


class ThreadExchangeFailedData(_DataModel):
    """`thread.exchange_failed.data` — closes one exchange with a Fatal error."""

    exchange_started_id: EventId
    error: Error


class UserMessageData(_DataModel):
    """`user.message.data` — one message the user sent into the exchange.

    `content` is typed per the agent's `IOSpec.inputs` (conversational
    mode), captured here as a permissive dict so the envelope is
    reusable across agents with differing schemas.
    """

    content: dict[str, Any]
    images: list[dict[str, Any]] | None = None


class AgentMessageData(_DataModel):
    """`agent.message.data` — the agent's reply that closes an exchange."""

    content: dict[str, Any]
    exchange_id: EventId


class ModelRequestData(_DataModel):
    """`model.request.data` — what the engine sent to the LLM for one call.

    `messages` carries the LLM-seam message list verbatim, captured
    post-framework-wrap and pre-LLM-invoke. The wrapping framework
    (CrewAI's prompt-template injection, multimodal reshaping) can
    diverge from what a path-derived context would produce, so the
    log keeps the actual bytes the model saw — the
    [Materialization Rule](../../docs/reference/master-index.md#key-vocabulary)
    requires every input that shaped the model's context to live on
    an event, verbatim. Replay reads `messages` directly rather than
    re-running the projection.

    `tools` carries the tool-schema list the LLM saw for this call.
    The model uses the schemas to decide whether to emit a tool-use
    response; capturing them keeps the log faithful to every input
    that shaped the model's behaviour. Empty list when the agent
    declares no tools; one entry per registered tool otherwise.
    """

    call_id: AgentCallId
    model: dict[str, Any]
    config_event_id: EventId
    messages: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)


class ModelResponseData(_DataModel):
    """`model.response.data` — what the LLM returned for one call."""

    call_id: AgentCallId
    content: dict[str, Any]
    reasoning_trace: dict[str, Any] | None = None
    usage: dict[str, Any]
    cost: float
    latency_ms: int
    stop_reason: str
    # When `stop_reason == "error"`, `error` carries the canonical
    # Error envelope. Optional otherwise.
    error: Error | None = None


class ModelFallbackData(_DataModel):
    """`model.fallback.data` — one transition in the model-fallback chain.

    Emitted by the engine once per fallback transition, between the
    failing attempt (its `model.request` or `model.response`) and the
    next attempt's `model.request`.

    Fields:

    - `from` / `to` — canonical `{provider, model}` dicts (the
      resolver normalises aliases before this event is emitted).
    - `reason` — the failing attempt's error code (e.g.
      `model.rate_limited`).
    - `reason_detail` — a one-line human-readable summary.
    - `attempt` — 1-indexed; attempt 0 was the primary, so the first
      `model.fallback` event always has `attempt: 1`.
    """

    # `from` shadows the Python keyword; use field-aliased form so the
    # JSON shape (and the on-disk JSONL) matches the canonical wire
    # format exactly. `populate_by_name` lets engine code construct
    # via the Python identifier (`from_model=`) while
    # `model_dump(by_alias=True)` round-trips to the canonical JSON.
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    from_model: dict[str, str] = Field(alias="from")
    to_model: dict[str, str] = Field(alias="to")
    reason: str
    reason_detail: str = ""
    attempt: int


class ToolCalledData(_DataModel):
    """`tool.called.data` — the agent's request to invoke one tool."""

    tool_name: str
    tool_call_id: str
    arguments: dict[str, Any]


class ToolResultData(_DataModel):
    """`tool.result.data` — what the tool returned for one call."""

    tool_call_id: str
    tool_name: str
    result: dict[str, Any]
    is_error: bool = False


class ToolFailedData(_DataModel):
    """`tool.failed.data` — a tool invocation that raised.

    When the failure was an authz denial (`error.type ==
    "authz.denied"` or the wrapped `tool.unauthorized` whose detail
    names a deny rule), the engine populates `recovery_hint` with the
    exact `/perm allow …` chat command the user can type to un-block.
    The chat renderer surfaces it inside the denied capsule; the
    framework forwards the whole event payload to the model so the
    agent sees the actionable text and stops flailing.

    `recovery_hint` is `None` for every non-authz failure — the field
    is purely a convenience layer for the authz path; other tool
    failures continue to surface through `error.message` alone.
    """

    tool_call_id: str
    tool_name: str
    error: Error
    recovery_hint: str | None = None


class ConfigSetData(_DataModel):
    """`config.set.data` — the resolved agent config for an exchange.

    `resolved_config` is the FULL, fully-dereferenced
    `ResolvedAgentConfig`; captured here as a permissive dict so this
    module has no import dependency on `schemas.config`. Callers that
    want the typed model use `ResolvedAgentConfig` directly.
    """

    resolved_config: dict[str, Any] = Field(min_length=1)
    config_hash: str


class MemoryWrittenData(_DataModel):
    """`memory.written.data` — one item the agent wrote to memory.

    The payload carries the full memory item record (not just `scope
    / content / variant`) so a replay can deterministically rebuild
    the in-memory cache from the log alone.
    """

    item_id: MemoryItemId
    agent_id: AgentId
    scope: str
    content: dict[str, Any]
    tags: list[str]
    metadata: dict[str, Any] = Field(default_factory=dict)
    variant: str


class MemoryRetrievedData(_DataModel):
    """`memory.retrieved.data` — what a memory query returned.

    `results` carries the actual items returned, verbatim. The
    [Assembly Procedure](../../docs/reference/master-index.md#key-vocabulary)
    uses this content; it never re-runs the query at replay time.
    """

    scope: str
    query: dict[str, Any]
    results: list[dict[str, Any]]
    variant: str


class MemoryDeletedData(_DataModel):
    """`memory.deleted.data` — one memory item the agent removed."""

    item_id: MemoryItemId


class ContextCompactedData(_DataModel):
    """`context.compacted.data` — one compaction the engine performed.

    The log keeps every original event; compaction only shortens what
    later model calls project as context. `pre_compaction` captures
    the FULL content being summarised, so a replay can decide whether
    to apply the compaction or skip it.
    """

    compacted_event_ids: list[EventId]
    pre_compaction: dict[str, Any]
    summary: dict[str, Any]
    tokens_before: int
    tokens_after: int


class AgentTemplateWarningData(_DataModel):
    """`agent.template_warning.data` — a template/config consistency warning.

    Emitted by the engine's pre-flight when a prompt references
    variable data the resolved config doesn't carry — e.g.
    `{{ tools }}` referenced but no tools declared.
    """

    reference: str
    reason: str
    hint: str


# ---------------------------------------------------------------------------
# The Event envelope
# ---------------------------------------------------------------------------


class Event(BaseModel):
    """The canonical Event envelope — every record on the JSONL log.

    `data` is intentionally loose (a `dict`). Callers that want strict
    per-type validation construct the matching `*Data` model and
    assign `.model_dump()` into `data`; the engine and store consume
    the envelope shape uniformly.

    `session_id` is carried even though the JSONL file already
    identifies the session — consumers reading events in memory
    shouldn't need to know the storage layout to attribute events.
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
    # Chain-spanning trace id. Set by the agent engine when its
    # `ExecutionContext` carries one (workflow-spawned agent
    # invocations); `None` for chat / API surfaces that haven't opted
    # in. Typed loosely as `str` (not `TraceId`) to keep `schemas/`
    # free of an upward dependency on `workflows/` — `TraceId` is a
    # `str` subclass so typed values still pass through transparently,
    # while plain strings read off legacy JSONL rows validate without
    # a prefix-check raise.
    trace_id: str | None = None
