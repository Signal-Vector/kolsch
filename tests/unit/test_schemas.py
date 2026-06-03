"""Unit tests for src/schemas — the cross-module data models.

Covers TC-SCH-* cases derived from the Phase 1 Schemas spec
(docs/reference/schemas.md, §B events / §C entities) and the Module
API Reference §13.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from errors import SecretNotFound, ToolSetupFailed
from schemas import (
    Actor,
    AgentCallId,
    AgentId,
    AgentMessageData,
    ConfigSetData,
    ContentRef,
    ContextCompactedData,
    Error,
    Event,
    EventId,
    EventType,
    IOSpec,
    MemoryDeletedData,
    MemoryItemId,
    MemoryRetrievedData,
    MemoryWrittenData,
    ModelRequestData,
    ModelResponseData,
    ModelSpec,
    PromptSpec,
    ResolvedAgentConfig,
    RuntimeSpec,
    Session,
    SessionId,
    SessionOpenedData,
    SubscriptionId,
    SubsystemConfig,
    Thread,
    ThreadExchangeCompletedData,
    ThreadExchangeFailedData,
    ThreadExchangeStartedData,
    ThreadId,
    ToolCalledData,
    ToolFailedData,
    ToolResultData,
    UserMessageData,
    derive_agent_id,
)

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _uuid() -> str:
    return str(uuid4())


def _ses() -> SessionId:
    return SessionId(f"ses_{_uuid()}")


def _th() -> ThreadId:
    return ThreadId(f"th_{_uuid()}")


def _ev() -> EventId:
    return EventId(f"ev_{_uuid()}")


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# TC-SCH-01 — prefixed identifiers: valid construction
# ---------------------------------------------------------------------------


def test_ids_construct_with_valid_prefix() -> None:
    u = _uuid()
    assert str(SessionId(f"ses_{u}")) == f"ses_{u}"
    assert str(ThreadId(f"th_{u}")) == f"th_{u}"
    assert str(EventId(f"ev_{u}")) == f"ev_{u}"
    assert str(MemoryItemId(f"mem_{u}")) == f"mem_{u}"
    assert str(AgentCallId(f"cl_{u}")) == f"cl_{u}"
    # AgentId body is user-chosen, not a UUID — both forms work.
    assert str(AgentId("ag_research-bot")) == "ag_research-bot"
    # SubscriptionId is opaque; only non-empty required.
    assert str(SubscriptionId("any-string-1")) == "any-string-1"


def test_ids_are_str_subclasses() -> None:
    # Inherits from str so dict keys, JSON encoders, and isinstance(...)
    # all behave naturally.
    sid = _ses()
    assert isinstance(sid, str)
    assert {sid: 1}[sid] == 1


# ---------------------------------------------------------------------------
# TC-SCH-02 — wrong prefix is rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cls", "bad_value"),
    [
        (SessionId, "th_abc"),
        (SessionId, "abc"),
        (ThreadId, "ses_abc"),
        (EventId, "ag_abc"),
        (MemoryItemId, "ses_abc"),
        (AgentCallId, "ev_abc"),
        (AgentId, "ses_abc"),
    ],
)
def test_id_rejects_wrong_prefix(cls: type, bad_value: str) -> None:
    with pytest.raises(ValueError, match="prefix"):
        cls(bad_value)


# ---------------------------------------------------------------------------
# TC-SCH-03 — empty body / malformed UUID rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cls", "bad_value"),
    [
        (SessionId, "ses_"),
        (ThreadId, "th_"),
        (EventId, "ev_"),
        (MemoryItemId, "mem_"),
        (AgentCallId, "cl_"),
        (AgentId, "ag_"),
    ],
)
def test_id_rejects_empty_body(cls: type, bad_value: str) -> None:
    with pytest.raises(ValueError, match="body"):
        cls(bad_value)


@pytest.mark.parametrize(
    "cls",
    [SessionId, ThreadId, EventId, MemoryItemId, AgentCallId],
)
def test_uuid_id_rejects_non_uuid_body(cls: type) -> None:
    # AgentId is exempt — its body is user-chosen, not a UUID.
    bad = f"{cls.prefix}not-a-uuid"  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="uuid"):
        cls(bad)


def test_agent_id_rejects_invalid_chars() -> None:
    # AgentId body must be identifier-like — no spaces, no slashes.
    with pytest.raises(ValueError, match="characters"):
        AgentId("ag_has spaces")
    with pytest.raises(ValueError, match="characters"):
        AgentId("ag_with/slash")


def test_derive_agent_id_format() -> None:
    # bug-agent-id-derive-from-ref.md Step 2: the helper composes
    # ag_<bundle>_<name> from the asset ref's (bundle, name) tuple
    # and returns an AgentId instance (so downstream typed fields
    # accept it without re-validation).
    derived = derive_agent_id("andre", "butler")
    assert derived == "ag_andre_butler"
    assert isinstance(derived, AgentId)
    # Hyphens in either segment survive the AgentId body regex.
    assert derive_agent_id("customer-support", "weekly-summary") == (
        "ag_customer-support_weekly-summary"
    )
    # Filename-stem with characters outside the AgentId body class
    # surfaces the underlying ValueError rather than producing a
    # malformed id — Step 1 §4 documented this as the failure mode.
    with pytest.raises(ValueError, match="characters"):
        derive_agent_id("andre", "has space")


def test_subscription_id_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        SubscriptionId("")


# ---------------------------------------------------------------------------
# TC-SCH-04 — identifiers validate when used as Pydantic fields
# ---------------------------------------------------------------------------


def test_ids_validate_inside_pydantic_models() -> None:
    # Passing a raw string to a model field invokes the validator.
    u = _uuid()
    cr = ContentRef(hash="a" * 64, size=10, stored_at=f"content/{'a' * 64}.bin")
    actor = Actor(kind="system")
    ev = Event(
        event_id=EventId(f"ev_{u}"),
        parent_event_id=None,
        thread_id=ThreadId(f"th_{u}"),
        session_id=SessionId(f"ses_{u}"),
        type=EventType.SESSION_OPENED,
        data={},
        actor=actor,
        created_at=_now(),
        content_ref=cr,
    )
    assert ev.event_id.startswith("ev_")


def test_pydantic_field_rejects_wrong_prefix() -> None:
    # Pydantic should refuse a string that doesn't validate as an EventId.
    with pytest.raises(ValidationError):
        Event(
            event_id="bad-id",  # type: ignore[arg-type]
            parent_event_id=None,
            thread_id=_th(),
            session_id=_ses(),
            type=EventType.SESSION_OPENED,
            data={},
            actor=Actor(kind="system"),
            created_at=_now(),
        )


# ---------------------------------------------------------------------------
# TC-SCH-05 — Event envelope and round-trip stability
# ---------------------------------------------------------------------------


def _make_event(event_type: EventType = EventType.USER_MESSAGE) -> Event:
    return Event(
        event_id=_ev(),
        parent_event_id=_ev(),
        thread_id=_th(),
        session_id=_ses(),
        type=event_type,
        data={"content": "hi"},
        actor=Actor(kind="user", id="user.local"),
        created_at=datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
        content_ref=None,
    )


def test_event_envelope_carries_all_fields() -> None:
    ev = _make_event()
    dumped = ev.model_dump()
    assert set(dumped.keys()) >= {
        "event_id",
        "parent_event_id",
        "thread_id",
        "session_id",
        "type",
        "data",
        "actor",
        "created_at",
        "content_ref",
    }
    assert dumped["type"] == "user.message"


def test_event_round_trip_is_stable() -> None:
    ev = _make_event()
    serialized = ev.model_dump_json()
    rebuilt = Event.model_validate_json(serialized)
    # Re-serialize the rebuilt model and compare bytes-for-bytes.
    assert rebuilt.model_dump_json() == serialized


def test_event_parent_is_optional_for_root() -> None:
    ev = _make_event()
    object.__setattr__(ev, "parent_event_id", None)
    assert ev.parent_event_id is None


def test_actor_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        Actor(kind="alien")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TC-SCH-06 — per-event-type data models reject malformed payloads
# ---------------------------------------------------------------------------


def test_session_opened_data() -> None:
    SessionOpenedData(
        session_id=_ses(),
        initial_agent={"id": "ag_x", "name": "x"},
    )
    with pytest.raises(ValidationError):
        SessionOpenedData(session_id="not-a-session-id")  # type: ignore[arg-type, call-arg]


def test_thread_exchange_started_data() -> None:
    ThreadExchangeStartedData(trigger_event_id=_ev())
    with pytest.raises(ValidationError):
        ThreadExchangeStartedData()  # type: ignore[call-arg]


def test_thread_exchange_completed_data() -> None:
    ThreadExchangeCompletedData(
        exchange_started_id=_ev(),
        agent_message_id=_ev(),
    )
    with pytest.raises(ValidationError):
        ThreadExchangeCompletedData(exchange_started_id=_ev())  # type: ignore[call-arg]


def test_thread_exchange_failed_data() -> None:
    err = ToolSetupFailed(tool_name="x", reason="y")
    ThreadExchangeFailedData(
        exchange_started_id=_ev(),
        error=Error.from_platform_error(err),
    )
    with pytest.raises(ValidationError):
        ThreadExchangeFailedData(  # type: ignore[call-arg]
            exchange_started_id=_ev(),
        )


def test_user_message_data_allows_typed_content() -> None:
    UserMessageData(content={"text": "hi"})
    UserMessageData(content={"text": "hi"}, images=[{"src": "data:..."}])
    with pytest.raises(ValidationError):
        UserMessageData()  # type: ignore[call-arg]


def test_agent_message_data() -> None:
    AgentMessageData(content={"text": "ok"}, exchange_id=_ev())
    with pytest.raises(ValidationError):
        AgentMessageData(content={"text": "ok"})  # type: ignore[call-arg]


def test_model_request_data() -> None:
    ModelRequestData(
        call_id=AgentCallId(f"cl_{_uuid()}"),
        model={"provider": "anthropic", "model": "claude-opus-4-7"},
        config_event_id=_ev(),
    )
    with pytest.raises(ValidationError):
        ModelRequestData(call_id="bad")  # type: ignore[arg-type, call-arg]


def test_model_response_data() -> None:
    ModelResponseData(
        call_id=AgentCallId(f"cl_{_uuid()}"),
        content={"text": "hi"},
        usage={"input_tokens": 10, "output_tokens": 5},
        cost=0.0001,
        latency_ms=200,
        stop_reason="end_turn",
    )
    with pytest.raises(ValidationError):
        ModelResponseData(
            call_id=AgentCallId(f"cl_{_uuid()}"),
            content={"text": "x"},
            cost="cheap",  # type: ignore[arg-type]
            usage={},
            latency_ms=1,
            stop_reason="end_turn",
        )


def test_tool_called_data() -> None:
    ToolCalledData(
        tool_name="read",
        tool_call_id="tc_123",
        arguments={"path": "data/x.md"},
    )
    with pytest.raises(ValidationError):
        ToolCalledData(tool_name="read")  # type: ignore[call-arg]


def test_tool_result_data() -> None:
    ToolResultData(
        tool_call_id="tc_123",
        tool_name="read",
        result={"text": "hello"},
        is_error=False,
    )
    with pytest.raises(ValidationError):
        ToolResultData(tool_call_id="tc")  # type: ignore[call-arg]


def test_tool_failed_data() -> None:
    err = SecretNotFound(name="MY_KEY")
    ToolFailedData(
        tool_call_id="tc_123",
        tool_name="read",
        error=Error.from_platform_error(err),
    )
    with pytest.raises(ValidationError):
        ToolFailedData(tool_call_id="tc", tool_name="read")  # type: ignore[call-arg]


def test_config_set_data() -> None:
    ConfigSetData(resolved_config={"agent_id": "ag_x"}, config_hash="abc123")
    with pytest.raises(ValidationError):
        ConfigSetData(resolved_config={})  # type: ignore[call-arg]


def test_memory_written_data() -> None:
    MemoryWrittenData(
        item_id=MemoryItemId(f"mem_{_uuid()}"),
        agent_id=AgentId("ag_test"),
        scope="research",
        content={"text": "fact"},
        tags=["notes/research"],
        metadata={"source": "user"},
        variant="recent_buffer",
    )
    with pytest.raises(ValidationError):
        # Missing required item_id/agent_id/tags fields.
        MemoryWrittenData(scope="x", content={"text": "y"})  # type: ignore[call-arg]


def test_memory_retrieved_data() -> None:
    MemoryRetrievedData(
        scope="research",
        query={"text": "what"},
        results=[{"text": "fact"}],
        variant="recent_buffer",
    )
    with pytest.raises(ValidationError):
        MemoryRetrievedData(scope="x")  # type: ignore[call-arg]


def test_memory_deleted_data() -> None:
    MemoryDeletedData(item_id=MemoryItemId(f"mem_{_uuid()}"))
    with pytest.raises(ValidationError):
        MemoryDeletedData(item_id="not-mem")  # type: ignore[arg-type]


def test_context_compacted_data() -> None:
    ContextCompactedData(
        compacted_event_ids=[_ev(), _ev()],
        pre_compaction={"events": ["raw"]},
        summary={"text": "summary"},
        tokens_before=1000,
        tokens_after=200,
    )
    with pytest.raises(ValidationError):
        ContextCompactedData(  # type: ignore[call-arg]
            compacted_event_ids=[_ev()],
            summary={"text": "x"},
        )


# ---------------------------------------------------------------------------
# TC-SCH-07 — Error envelope wired to PlatformError.to_dict()
# ---------------------------------------------------------------------------


def test_error_round_trips_through_platform_error() -> None:
    try:
        try:
            raise SecretNotFound(name="MY_KEY")
        except SecretNotFound as inner:
            raise ToolSetupFailed(
                tool_name="web_search", reason="secret missing"
            ) from inner
    except ToolSetupFailed as outer:
        envelope = outer.to_dict()
        error_model = Error.model_validate(envelope)

    # Round-trip equality: the Pydantic model serializes back to the
    # same envelope shape PlatformError.to_dict() emits.
    assert error_model.model_dump() == envelope
    assert error_model.type == "tool.setup_failed"
    assert error_model.cause is not None
    assert error_model.cause.type == "secret.not_found"
    assert error_model.cause.cause is None


def test_error_recursive_cause_chain_serializes() -> None:
    nested = Error(
        type="a.x",
        message="outer",
        where="x.x",
        detail={},
        cause=Error(
            type="b.x",
            message="middle",
            where="x.x",
            detail={},
            cause=Error(
                type="c.x",
                message="inner",
                where="x.x",
                detail={},
                cause=None,
            ),
        ),
    )

    serialized = nested.model_dump_json()
    rebuilt = Error.model_validate_json(serialized)
    assert rebuilt.model_dump_json() == serialized


# ---------------------------------------------------------------------------
# Entity records (§C)
# ---------------------------------------------------------------------------


def test_session_entity_record() -> None:
    s = Session(
        session_id=_ses(),
        agent_id=AgentId("ag_x"),
        name="my session",
        starred=False,
        archived=False,
        created_at=_now(),
        updated_at=_now(),
    )
    assert s.starred is False
    rebuilt = Session.model_validate_json(s.model_dump_json())
    assert rebuilt.session_id == s.session_id


def test_thread_entity_record_includes_agent_id() -> None:
    """Schemas §C lists Thread.agent_id; Module API Reference §3.2
    omits it. The entity-record spec wins for entity records."""
    t = Thread(
        thread_id=_th(),
        session_id=_ses(),
        agent_id=AgentId("ag_x"),
        created_at=_now(),
    )
    assert t.agent_id == "ag_x"


def test_content_ref_accepts_valid_hex_hash() -> None:
    ContentRef(hash="a" * 64, size=128, stored_at=f"content/{'a' * 64}.bin")


def test_content_ref_rejects_negative_size() -> None:
    with pytest.raises(ValidationError):
        ContentRef(hash="a" * 64, size=-1, stored_at="content/x.bin")


def test_content_ref_rejects_short_hash() -> None:
    # 15 chars — fails because it's not 64 chars; the validator's
    # length+hex regex catches both length and character-set issues.
    with pytest.raises(ValidationError) as exc_info:
        ContentRef(hash="not_hex_at_all!", size=128, stored_at="content/x.bin")
    assert "SHA-256" in str(exc_info.value)


def test_content_ref_rejects_correct_length_but_non_hex_chars() -> None:
    # 64 chars but with non-hex characters — this is the case the prior
    # `min_length=64` constraint would have hidden: a value the wrong
    # length would fail length before the hex check ever ran. The
    # `field_validator` regex now enforces both in one check, so this
    # input fires the hex branch specifically.
    with pytest.raises(ValidationError) as exc_info:
        ContentRef(hash="z" * 64, size=128, stored_at="content/z.bin")
    assert "SHA-256" in str(exc_info.value)


# ---------------------------------------------------------------------------
# ResolvedAgentConfig + sub-types (§A)
# ---------------------------------------------------------------------------


def test_resolved_agent_config_minimal() -> None:
    cfg = ResolvedAgentConfig(
        schema_version="0.1",
        assembly_version="0.1",
        agent_id=AgentId("ag_x"),
        name="x",
        description="d",
        model=ModelSpec(provider="anthropic", model="claude-opus-4-7"),
        prompt="You are an agent.",
        io=IOSpec(mode="conversational"),
        subsystems={},
        runtime=RuntimeSpec(),
    )
    assert cfg.prompt == "You are an agent."


def test_resolved_agent_config_with_prompt_list() -> None:
    cfg = ResolvedAgentConfig(
        schema_version="0.1",
        assembly_version="0.1",
        agent_id=AgentId("ag_x"),
        name="x",
        description="d",
        model=ModelSpec(provider="anthropic", model="claude-opus-4-7"),
        prompt=["You are.", "Be helpful."],
        io=IOSpec(mode="single_shot"),
        subsystems={"memory": SubsystemConfig(variant="recent_buffer")},
    )
    assert isinstance(cfg.prompt, list)
    assert cfg.subsystems["memory"].variant == "recent_buffer"


def test_io_spec_mode_constrained() -> None:
    IOSpec(mode="conversational")
    IOSpec(mode="single_shot")
    with pytest.raises(ValidationError):
        IOSpec(mode="weird")  # type: ignore[arg-type]


def test_prompt_spec_alias_accepts_both_shapes() -> None:
    # PromptSpec is a union; this test documents the expected forms.
    a: PromptSpec = "inline string"
    b: PromptSpec = ["layer1", "layer2"]
    assert isinstance(a, str)
    assert isinstance(b, list)
