"""Event envelope plus the per-type data payload models.

Event types are organised as a [`StrEnum`][schemas.events.EventType] so
emitters get autocomplete plus Pydantic validation, and consumers can
exhaustively switch over the set.

Each event type has a dedicated `*Data` Pydantic model declaring its
payload shape. The [`Event`][schemas.events.Event] envelope holds
`data` as a permissive `dict[str, Any]` so the on-disk JSONL stays
forward-compatible; callers that want strict validation construct the
matching `*Data` model and pass `.model_dump()` into the envelope.

For the full event-log contract — the append protocol, assembly
procedure, materialization rule, and reconstruction guarantee — see
the [Event log contract](../explanation/event-log-contract.md).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
from schemas.wire_kinds import is_valid_kind


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
    # Per-chunk delta emitted by streaming-capable framework paths
    # between `MODEL_REQUEST` and `MODEL_RESPONSE`. Non-streaming
    # framework paths emit zero of these; the whole response still
    # arrives on `MODEL_RESPONSE`. See
    # `process/spec-textual-chat-ui.md` for the wire contract.
    MODEL_STREAMING_TOKEN = "model.streaming_token"  # noqa: S105 — event kind, not a secret
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
    CONTEXT_COMPACTION_REVOKED = "context.compaction_revoked"
    CONTEXT_COMPACTION_FAILED = "context.compaction_failed"
    CONTEXT_COMPACTION_SKIPPED = "context.compaction_skipped"

    # Engine pre-flight emits one of these when a template references
    # variable data the resolved config doesn't carry (e.g.
    # `{{ tools }}` referenced but no tools declared).
    AGENT_TEMPLATE_WARNING = "agent.template_warning"

    # UI mutation envelope emitted by the tool-services layer
    # (CardsImpl, PromptsImpl, WorkflowsImpl). Discriminated by
    # `UIEventData.kind` (a dotted `kolsch.<domain>.<verb>` string)
    # rather than by adding one `EventType` member per UI verb. See
    # `src/schemas/wire_kinds.py` for the canonical platform set.
    UI_EVENT = "ui.event"


# ---------------------------------------------------------------------------
# Per-event-type `data` payload models
# ---------------------------------------------------------------------------


class _DataModel(BaseModel):
    """Common base for event-data payloads.

    `extra="ignore"` (relaxed from the pre-spec-002 `extra="forbid"`)
    keeps forward-compat replay tolerant: newer JSONL entries with
    added payload fields don't fail validation on older readers. See
    the roadmap's cross-cutting invariants and
    `process/spec-002-ui-event-schema.md` for the rationale.

    Every payload subclass inherits `logical_seq: int = 0`, the
    cross-log ordering key populated by :class:`SessionStore` inside
    its flock critical section (allocation logic lands in spec-003).
    Default `0` means legacy events without the field validate
    cleanly on replay.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    # spec-002: field-in-place; spec-003 populates on write. Default
    # zero keeps legacy log entries validating without a migration.
    logical_seq: int = 0


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
    log keeps the actual bytes the model saw — the [Materialization
    Rule](../explanation/event-log-contract.md#the-materialization-rule)
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


class ModelStreamingTokenData(_DataModel):
    """`model.streaming_token.data` — one chunk of a streamed model response.

    Emitted by streaming-capable framework paths between the
    `MODEL_REQUEST` and `MODEL_RESPONSE` for the same `call_id`. The
    concatenation of every delta for a given `call_id` (in event
    order) must equal the `content` text on the final
    `MODEL_RESPONSE`; consumers verify this defensively.
    Non-streaming framework paths emit zero deltas — behaviour is
    unchanged and the whole response arrives on `MODEL_RESPONSE`.

    Fields are minimal by design. If a future spec needs to
    distinguish extended-thinking tokens from assistant tokens it
    can either add a `channel` field via migration or introduce a
    separate `MODEL_THINKING_TOKEN` event; both paths are cheap and
    premature reservation isn't warranted (see
    `process/spec-textual-chat-ui.md` Coordinator decision 1).
    """

    call_id: AgentCallId
    text_delta: str


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

    `recovery_hint` (when set) is the exact command a user should run
    to unblock the failure. The chat renderer surfaces it inside the
    failure capsule; the framework forwards the whole event payload
    to the model so the agent sees the actionable text and stops
    flailing. Two shapes populate it:

    - **Authz denial** — the engine's authz arm computes a `/perm
      allow <tool> <pattern>` command from the matched rule and the
      in-flight args.
    - **Tool exception carrying `.recovery_hint`** (qol-090) — any
      `ToolExecutionFailed` that opts in via the `recovery_hint`
      constructor kwarg; the observer's generic `PlatformError` arm
      forwards it. `errors.recovery.recovery_hint()` is the canonical
      builder — it detects the install mode and returns the exact
      install command per registered `error_class`.

    `recovery_hint` is `None` for any failure that neither opts in.
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
    [Assembly Procedure](../explanation/event-log-contract.md#the-assembly-procedure)
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

    `revoked_by` points at a `context.compaction_revoked` event that
    invalidated this compaction (from `/uncompact`); Assembly filters
    both when projecting. `preview` is `True` when this compaction was
    emitted as a diagnostic without swapping the live context.
    `trigger` records what fired the compaction: `"auto"` for
    threshold-triggered during an in-flight exchange, `"auto_resume"`
    for the feat-054 Phase 5 session-open eager check (fires BEFORE
    the user's first post-resume message so the summariser delay is
    absorbed into the resume UX instead of the first exchange),
    `"auto_resume_preview"` for the read-only variant of the eager
    check, `"manual"` for a user-invoked `/compact`, and
    `"manual_preview"` for `/compact --preview`. The two `*_truncation`
    values mark a *mechanical* (non-LLM) truncation fallback rather than
    an LLM summary: `"auto_truncation"` when the auto/eager ladder's
    summariser was exhausted, and `"manual_truncation"` when a
    user-invoked `/compact` fell back to mechanical truncation (e.g. the
    middle span overflowed the summariser's context window).
    """

    compacted_event_ids: list[EventId]
    pre_compaction: dict[str, Any]
    summary: dict[str, Any]
    tokens_before: int
    tokens_after: int
    revoked_by: EventId | None = None
    preview: bool = False
    trigger: Literal[
        "auto",
        "auto_resume",
        "auto_resume_preview",
        "manual",
        "manual_preview",
        "auto_truncation",
        "manual_truncation",
    ] = "auto"


class ContextCompactionRevokedData(_DataModel):
    """`context.compaction_revoked.data` — one `/uncompact` invocation.

    Points at the `context.compacted` event this revocation
    invalidates. Assembly filters both events from projection so the
    live model's next turn sees the restored (verbatim) context.
    """

    compaction_event_id: EventId
    reason: str = "user_requested"


class ContextCompactionFailedData(_DataModel):
    """`context.compaction_failed.data` — a summariser LLM/JSON failure.

    Emitted when the summariser LLM call raises twice or the returned
    payload fails JSON validation. The engine follows the failure
    ladder (mechanical truncation) and continues the exchange; this
    event records the diagnostic so `/why-compact` can surface it.
    """

    error: Error
    span_first_event_id: EventId | None = None
    span_last_event_id: EventId | None = None


class ContextCompactionSkippedData(_DataModel):
    """`context.compaction_skipped.data` — a compaction attempt was discarded.

    Emitted at the compaction pre-flight. Documented reasons:

    - ``no_net_savings`` — the summariser ran but produced no worthwhile
      shrink (``tokens_after > tokens_before * 0.90``). ``tokens_before``
      / ``tokens_after`` carry the observed sizes.
    - ``no_context_length`` — the model has no declared ``context_length``
      and no cross-provider metadata source (see qol-091) could supply
      one, so the compaction budget is uncomputable. ``detail`` carries
      the model spec and an operator-facing remediation hint.
    - ``unknown`` — historical/replay fallback when the reason string
      is missing from the payload.

    ``detail`` is an optional string-valued map for reason-specific
    context (added by qol-092 for ``no_context_length``: ``provider``,
    ``model``, ``remediation_hint``). Consumers should treat unknown
    keys as opaque strings and ignore missing keys. Non-string values
    (numbers, nested dicts, lists) would need a schema widen — keep
    keys/values as strings, or bump the field type when a genuine
    richer shape is needed.
    """

    reason: str
    tokens_before: int = 0
    tokens_after: int = 0
    detail: dict[str, str] | None = None


class AgentTemplateWarningData(_DataModel):
    """`agent.template_warning.data` — a template/config consistency warning.

    Emitted by the engine's pre-flight when a prompt references
    variable data the resolved config doesn't carry — e.g.
    `{{ tools }}` referenced but no tools declared.
    """

    reference: str
    reason: str
    hint: str


class UIEventData(_DataModel):
    """`ui.event.data` — one UI mutation.

    `kind` is the discriminator: a dotted `<owner>.<domain>.<verb>`
    string (see :mod:`schemas.wire_kinds` for the platform set). Every
    subsequent UI reader switches on `kind` to reach the
    kind-specific `payload` shape.

    `schema_version` enables forward-compatible payload evolution:
    older readers ignore unknown extra fields (see
    :attr:`_DataModel.model_config`); a semantic break bumps the int
    and adds a migration handler (deferred until the first migration
    is needed — see spec-002 §Coordinator decisions).

    Kinds MUST be dotted `<owner>.<domain>.<verb>`:

    - `<owner>` is `kolsch` (platform-emitted) or a bundle name
      (v1.1). The platform reserves `kolsch.*`.
    - Non-dotted kinds fail construction via :func:`is_valid_kind`.
    - Deeper hierarchies are allowed
      (e.g. `kolsch.workflow.node.awaiting_approval`).
    """

    schema_version: int = 1
    kind: str
    payload: dict[str, Any]

    @field_validator("kind")
    @classmethod
    def _validate_dotted(cls, v: str) -> str:
        if not is_valid_kind(v):
            msg = (
                f"kind must be dotted <owner>.<domain>.<verb>; got {v!r}. "
                "See src/schemas/wire_kinds.py for the platform set "
                "(all `kolsch.*`). Bundle authors: prefix with your "
                "bundle name."
            )
            raise ValueError(msg)
        return v


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
