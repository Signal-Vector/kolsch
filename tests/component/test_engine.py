"""Component tests for src/engine — TC-ENG-01..11.

Spec: docs/explanation/agent-engine-contract.md + Module API Reference
§11. The Test Catalog (draft 0.2) reclassifies TC-ENG-01..11 as
framework-agnostic: this file runs them against
:class:`engine.MinimalFramework` (Step 12a). Step 12b will re-run the
same cases against ``CrewAIFramework``; the golden event sequences
must match.

These tests run real SessionStore (SQLite), real EventBus
(in-process), real SkillsSubsystem, real ToolsSubsystem, real
providers — only the LLM and the agentic framework are stubbed.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
import yaml
from pydantic import BaseModel

from assets import AssetManagement
from bus import InProcessEventBus
from engine import (
    AgentEngine,
    AgentExecutionError,
    CancelToken,
    ExecutionContext,
    LLMRequest,
    LLMResponse,
    MinimalFramework,
    assemble_context,
)
from files import PathPolicy
from providers import (
    NativeSandboxProvider,
    RecentBufferMemoryProvider,
    RuleBasedAuthorizationProvider,
)
from providers.secrets import DotenvSecretsProvider
from schemas import (
    AgentId,
    EventType,
    IOSpec,
    ModelSpec,
    ResolvedAgentConfig,
    RuntimeSpec,
    SubsystemConfig,
)
from skills import SkillsSubsystem
from store import SessionStore, SqliteBackend
from tests.fixtures.fake_llm import (
    FakeLLM,
    FakeLLMScriptedError,
    TextResponse,
    ToolCallResponse,
)
from tests.golden._helpers import assert_matches_golden
from tools import ToolContext, ToolsSubsystem, tool

if TYPE_CHECKING:
    from pathlib import Path

    from schemas import Event


# ---------------------------------------------------------------------------
# Test helpers — build a fully-wired engine on a tmp_path
# ---------------------------------------------------------------------------


def _platform_yaml(root: Path) -> None:
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (root / sub).mkdir(exist_ok=True)
    (root / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
                "sandbox": {"variant": "native", "config": {}},
                "authorization": {
                    "variant": "rule_based",
                    "config": {
                        "rules": [{"who": ".*", "what": ".*", "effect": "allow"}],
                    },
                },
                "storage": {
                    "entity_backend": "sqlite",
                    "entity_path": ".platform/entities.db",
                },
                "event_bus": {"backend": "in_process"},
                "file_access": {
                    "allowed_paths": ["data/", "bundles/"],
                    "denied_paths": [".env"],
                },
                "defaults": {},
            }
        ),
        encoding="utf-8",
    )
    (root / ".env").write_text("", encoding="utf-8")


def _config(agent_id: str = "ag_test") -> ResolvedAgentConfig:
    return ResolvedAgentConfig(
        schema_version="0.1",
        assembly_version="1",
        agent_id=AgentId(agent_id),
        name="test-agent",
        description="A test agent.",
        model=ModelSpec(provider="fake", model="fake-model"),
        prompt="You are a helpful test agent.",
        io=IOSpec(mode="conversational"),
    )


def _config_with_tool() -> ResolvedAgentConfig:
    cfg = _config()
    return cfg.model_copy(
        update={
            "subsystems": {
                "tools": SubsystemConfig(
                    variant="standard",
                    config={"builtin": []},
                ),
            },
        },
    )


async def _build_engine(
    tmp_path: Path,
    fake_llm: FakeLLM,
    *,
    authz_rules: list[dict[str, str]] | None = None,
    extra_tools: list[Any] | None = None,
) -> tuple[AgentEngine, SessionStore, AssetManagement]:
    _platform_yaml(tmp_path)
    backend = SqliteBackend(tmp_path / ".platform" / "entities.db")
    await backend.initialize()
    store = SessionStore(root=tmp_path, backend=backend)

    am = AssetManagement(root=tmp_path)
    await am.initialize()

    rules_cfg = authz_rules or [{"who": ".*", "what": ".*", "effect": "allow"}]
    authz = RuleBasedAuthorizationProvider(config={"rules": rules_cfg})

    secrets = DotenvSecretsProvider(root=tmp_path, config={"path": ".env"})
    sandbox = NativeSandboxProvider()
    memory = RecentBufferMemoryProvider(store=store)

    path_policy = PathPolicy(
        allowed_paths=["data/", "bundles/"],
        denied_paths=[".env"],
    )
    tools_sub = ToolsSubsystem(
        secrets=secrets,
        sandbox=sandbox,
        authz=authz,
        path_policy=path_policy,
        root=tmp_path,
    )

    if extra_tools:
        await tools_sub.register_for_agent(
            agent_id=AgentId("ag_test"),
            tools=extra_tools,
        )

    skills_sub = SkillsSubsystem(asset_management=am)
    bus = InProcessEventBus()

    framework = MinimalFramework()

    async def llm_callable(request: LLMRequest) -> LLMResponse:
        # Adapt LLMRequest → FakeLLM's dict context shape; translate
        # the Fake's response variants back to LLMResponse.
        context: dict[str, Any] = {
            "messages": [m.model_dump() for m in request.messages],
            "tools": list(request.tools),
            "model": request.model,
            "system_prompt": next(
                (m.content for m in request.messages if m.role == "system"),
                "",
            ),
        }
        try:
            fake_response = await fake_llm.agenerate(context)
        except FakeLLMScriptedError as exc:
            # Real LLM adapters (Step 12b CrewAI) translate provider
            # errors into the typed catalog. Mirror that here so engine
            # retry/fatal logic sees the right Level.
            from errors import ModelUpstreamError

            if exc.error_type == "model.upstream_error":
                raise ModelUpstreamError(
                    provider="fake",
                    http_status=500,
                    response=str(exc),
                ) from exc
            raise
        if isinstance(fake_response, TextResponse):
            return LLMResponse(content=fake_response.text)
        if isinstance(fake_response, ToolCallResponse):
            return LLMResponse(
                content=None,
                tool_calls=[
                    {
                        "id": fake_response.tool_call_id or f"tc_{uuid4()}",
                        "name": fake_response.tool,
                        "arguments": dict(fake_response.args),
                    },
                ],
            )
        msg = f"unsupported fake response: {fake_response!r}"
        raise RuntimeError(msg)

    engine = AgentEngine(
        store=store,
        bus=bus,
        asset_management=am,
        tools_subsystem=tools_sub,
        skills_subsystem=skills_sub,
        memory_provider=memory,
        secrets=secrets,
        sandbox=sandbox,
        authz=authz,
        framework=framework,
        llm=llm_callable,
    )
    return engine, store, am


def _events_before_first_model_request(events: list[Event]) -> list[Event]:
    out: list[Event] = []
    for evt in events:
        if evt.type == EventType.MODEL_REQUEST:
            return out
        out.append(evt)
    return out


def _events_before_last_model_request(events: list[Event]) -> list[Event]:
    """Path AS IT WAS just before the most recent model.request."""
    last_idx = -1
    for i, evt in enumerate(events):
        if evt.type == EventType.MODEL_REQUEST:
            last_idx = i
    if last_idx < 0:
        return list(events)
    return events[:last_idx]


def _as_messages(raw: object) -> list[dict[str, Any]]:
    """Help mypy: the FakeLLM stores context as dict[str, Any]; the
    'messages' entry is concretely a list of message dicts."""
    if not isinstance(raw, list):
        msg = f"expected messages list, got {type(raw).__name__}"
        raise TypeError(msg)
    out: list[dict[str, Any]] = []
    for item in raw:
        assert isinstance(item, dict)
        out.append(item)
    return out


async def _new_session_context(store: SessionStore) -> ExecutionContext:
    session = await store.create_session("ag_test", name="test-session")
    threads = await store.list_threads(session.session_id)
    return ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": "Hello, agent."},
    )


# ===========================================================================
# QoL — assembly_version capability check still fires after Step 2.
# ===========================================================================


async def test_preflight_rejects_unsupported_assembly_version(
    tmp_path: Path,
) -> None:
    """Engine pre-flight Step 2 raises CapabilityMismatch when the
    resolved config declares an assembly_version the engine doesn't
    support. Regression for ``qol-yaml-version-fields-optional.md``
    Step 2: the resolved-layer default ("1") must not paper over the
    load-bearing comparison at ``engine.py:_preflight``.
    """
    import asyncio

    from engine.engine import _Exchange
    from errors import CapabilityMismatch

    fake = FakeLLM(script=[])
    engine, store, _am = await _build_engine(tmp_path, fake)
    ctx = await _new_session_context(store)

    bad_config = _config().model_copy(update={"assembly_version": "999"})
    exchange = _Exchange(
        engine=engine,
        config=bad_config,
        context=ctx,
        inputs={},
        queue=asyncio.Queue(),
    )
    with pytest.raises(CapabilityMismatch):
        await exchange._preflight()


# ===========================================================================
# TC-ENG-01 — Simple exchange golden sequence
# ===========================================================================


_GOLDEN_SIMPLE_TYPES = (
    EventType.CONFIG_SET,
    EventType.THREAD_EXCHANGE_STARTED,
    EventType.USER_MESSAGE,
    EventType.MODEL_REQUEST,
    EventType.MODEL_RESPONSE,
    EventType.AGENT_MESSAGE,
    EventType.THREAD_EXCHANGE_COMPLETED,
)


async def test_tc_eng_01_simple_exchange_golden_sequence(tmp_path: Path) -> None:
    fake = FakeLLM(script=[TextResponse(text="Hi back.")])
    engine, store, _am = await _build_engine(tmp_path, fake)
    ctx = await _new_session_context(store)

    result = await engine.execute(_config(), ctx)

    assert result.type == EventType.AGENT_MESSAGE
    assert result.data["content"]["text"] == "Hi back."

    # Type-tuple smoke check.
    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    engine_events = [e for e in events if e.type != EventType.SESSION_OPENED]
    assert tuple(e.type for e in engine_events) == _GOLDEN_SIMPLE_TYPES

    # Byte-level golden — diff against tests/golden/tc_eng_01_simple.jsonl.
    # Regenerate via UPDATE_GOLDEN=1; review the diff before committing.
    assert_matches_golden("tc_eng_01_simple", engine_events)


# ===========================================================================
# Phase 3.2 §11.6.4 — trace_id propagates from ExecutionContext to every
# event the engine emits (cross-JSONL correlation for workflow-spawned
# agent invocations).
# ===========================================================================


async def test_execution_context_trace_id_stamped_on_every_engine_event(
    tmp_path: Path,
) -> None:
    """The agent runner sets ``ExecutionContext.trace_id`` from the
    workflow run's ``trace_id``. The engine's ``_emit`` chokepoint
    stamps it on every Event so the spawned session's JSONL and the
    originating workflow's JSONL share a single trace_id across all
    events. Master spec §11.6.4."""
    from workflows.ids import TraceId

    trace_id = TraceId(f"tr_{uuid4()}")

    fake = FakeLLM(script=[TextResponse(text="Done.")])
    engine, store, _am = await _build_engine(tmp_path, fake)
    session = await store.create_session(
        "ag_test",
        name="trace-test",
        trace_id=str(trace_id),
    )
    threads = await store.list_threads(session.session_id)
    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": "ping."},
        trace_id=trace_id,
    )

    await engine.execute(_config(), ctx)

    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    assert events  # sanity — exchange produced events
    for event in events:
        assert event.trace_id == str(trace_id), (
            f"event {event.type} carries trace_id={event.trace_id!r}, "
            f"expected {trace_id!r}"
        )


async def test_execution_context_trace_id_none_preserves_legacy_shape(
    tmp_path: Path,
) -> None:
    """Chat / API callers leave ``trace_id`` ``None``; existing
    behaviour is unchanged and the on-disk JSONL omits the key
    entirely (verified separately by the simple-exchange golden test).
    Here we just assert the in-memory Event field stays ``None`` so
    consumers of the API don't observe an accidental promotion."""
    fake = FakeLLM(script=[TextResponse(text="Done.")])
    engine, store, _am = await _build_engine(tmp_path, fake)
    ctx = await _new_session_context(store)  # no trace_id

    await engine.execute(_config(), ctx)

    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    for event in events:
        assert event.trace_id is None, (
            f"event {event.type} unexpectedly carries trace_id={event.trace_id!r}"
        )


# ===========================================================================
# TC-ENG-02 — Tool-call exchange golden sequence
# ===========================================================================


class _PingArgs(BaseModel):
    message: str


@tool(
    name="ping",
    description="Echo a message.",
    args_schema=_PingArgs,
)
async def _ping_tool(args: _PingArgs, ctx: ToolContext) -> dict[str, Any]:
    return {"echoed": args.message}


_GOLDEN_TOOL_TYPES = (
    EventType.CONFIG_SET,
    EventType.THREAD_EXCHANGE_STARTED,
    EventType.USER_MESSAGE,
    EventType.MODEL_REQUEST,
    EventType.MODEL_RESPONSE,
    EventType.TOOL_CALLED,
    EventType.TOOL_RESULT,
    EventType.MODEL_REQUEST,
    EventType.MODEL_RESPONSE,
    EventType.AGENT_MESSAGE,
    EventType.THREAD_EXCHANGE_COMPLETED,
)


async def test_tc_eng_02_tool_call_exchange_golden_sequence(tmp_path: Path) -> None:
    fake = FakeLLM(
        script=[
            ToolCallResponse(
                tool="ping",
                args={"message": "ack"},
                tool_call_id="call_ping",
            ),
            TextResponse(text="Tool said ack."),
        ],
    )
    engine, store, _am = await _build_engine(
        tmp_path,
        fake,
        extra_tools=[_ping_tool],
    )
    ctx = await _new_session_context(store)

    result = await engine.execute(_config_with_tool(), ctx)
    assert "ack" in result.data["content"]["text"]

    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    engine_events = [e for e in events if e.type != EventType.SESSION_OPENED]
    assert tuple(e.type for e in engine_events) == _GOLDEN_TOOL_TYPES

    # Byte-level golden — diff against tests/golden/tc_eng_02_tool_call.jsonl.
    assert_matches_golden("tc_eng_02_tool_call", engine_events)


# ===========================================================================
# TC-ENG-03 — Materialization check (3 scenarios)
# ===========================================================================


async def test_tc_eng_03_materialization_simple_text(tmp_path: Path) -> None:
    """Scenario 1: text-only exchange. ``model.request.data.messages``
    captures what the LLM saw verbatim (§B.6 draft-0.6 amendment) AND
    the Assembly Procedure on the truncated path produces the same
    projection (replay-by-derivation still works for non-wrapping
    frameworks like MinimalFramework)."""
    fake = FakeLLM(script=[TextResponse(text="Done.")])
    engine, store, _am = await _build_engine(tmp_path, fake)
    ctx = await _new_session_context(store)

    await engine.execute(_config(), ctx)

    assert len(fake.calls) == 1
    seen_messages = _as_messages(fake.calls[0].context["messages"])

    all_events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]

    # §B.6 amendment — the canonical replay source. The captured
    # messages on the model.request event MUST equal what the LLM
    # saw. This is the load-bearing check; it holds under any
    # framework that goes through the LLMHandle seam.
    request_events = [e for e in all_events if e.type == EventType.MODEL_REQUEST]
    assert len(request_events) == 1
    captured = request_events[0].data["messages"]
    assert captured == seen_messages

    # Assembly Procedure on the path AS IT WAS at the moment of the
    # model call (truncated at the first model.request) — derivation
    # also produces the same projection under MinimalFramework (no wrap).
    truncated = _events_before_first_model_request(all_events)
    assembled = assemble_context(truncated)
    derived_full = [
        {
            "role": "system",
            "content": assembled.system_prompt,
            "tool_call_id": None,
            "tool_calls": None,
            "name": None,
        },
        *(m.model_dump() for m in assembled.messages),
    ]
    assert derived_full == seen_messages


async def test_tc_eng_03_materialization_tool_call(tmp_path: Path) -> None:
    """Scenario 2: tool-call exchange. Two model.request events fire
    (one per turn); each captures the LLM-seam messages for that
    turn. Replay sources from the event — Assembly Procedure on the
    truncated path also reproduces the MinimalFramework view as a
    secondary check."""
    fake = FakeLLM(
        script=[
            ToolCallResponse(tool="ping", args={"message": "x"}),
            TextResponse(text="ok"),
        ],
    )
    engine, store, _am = await _build_engine(
        tmp_path,
        fake,
        extra_tools=[_ping_tool],
    )
    ctx = await _new_session_context(store)

    await engine.execute(_config_with_tool(), ctx)

    # The final LLM call saw [system, user, assistant_with_tool_call, tool_result].
    final_messages = _as_messages(fake.calls[-1].context["messages"])
    roles = [m["role"] for m in final_messages]
    assert roles == ["system", "user", "assistant", "tool"]

    all_events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]

    # §B.6 amendment — the captured LLM-seam messages on the LAST
    # model.request equal what the Fake LLM saw on its FINAL call.
    request_events = [e for e in all_events if e.type == EventType.MODEL_REQUEST]
    assert len(request_events) == 2
    last_captured = request_events[-1].data["messages"]
    assert last_captured == final_messages

    # Assembly Procedure on the truncated path also reproduces it
    # (MinimalFramework doesn't wrap, so derivation matches capture).
    truncated = _events_before_last_model_request(all_events)
    assembled = assemble_context(truncated)
    derived_full = [
        {
            "role": "system",
            "content": assembled.system_prompt,
            "tool_call_id": None,
            "tool_calls": None,
            "name": None,
        },
        *(m.model_dump() for m in assembled.messages),
    ]
    assert derived_full == final_messages


async def test_tc_eng_03_materialization_with_layered_prompt(
    tmp_path: Path,
) -> None:
    """Scenario 3: layered PromptSpec round-trips through the Assembly
    Procedure. Renamed from the misleading ``..._with_memory`` since
    the body never exercised memory — TC-ENG-03 scenario 4 below is
    the real memory case."""
    fake = FakeLLM(script=[TextResponse(text="OK.")])
    cfg = _config().model_copy(
        update={"prompt": ["Layer 1.", "Layer 2."]},
    )
    engine, store, _am = await _build_engine(tmp_path, fake)
    ctx = await _new_session_context(store)
    await engine.execute(cfg, ctx)

    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    assembled = assemble_context(events)

    # The multi-layer prompt is concatenated with \n\n per the
    # Assembly Procedure §B.5 step 3.
    assert "Layer 1." in assembled.system_prompt
    assert "Layer 2." in assembled.system_prompt
    # That same system prompt is what the Fake saw.
    assert assembled.system_prompt == fake.calls[0].context["system_prompt"]


async def test_tc_eng_03_materialization_with_memory(tmp_path: Path) -> None:
    """Scenario 4: a ``memory.retrieved`` event on the path projects
    its captured content verbatim into the model's context (§B.6
    "Memory retrievals — memory.retrieved events carry the actual
    content returned by the retrieval, verbatim"). The Assembly
    Procedure replays the captured content; it never re-runs the
    retrieval. This test pins the projection contract directly: emit
    a memory.retrieved event into the log before the exchange, then
    verify the Fake LLM's recorded context contains the verbatim
    content."""
    fake = FakeLLM(script=[TextResponse(text="Got it.")])
    engine, store, _am = await _build_engine(tmp_path, fake)
    ctx = await _new_session_context(store)

    # Inject a memory.retrieved event into the log mid-thread, BEFORE
    # the exchange runs. The Assembly Procedure will project it as a
    # system "context injection" per §B.6.
    from datetime import UTC, datetime
    from uuid import uuid4

    from schemas import (
        Actor,
        Event,
        EventId,
        MemoryRetrievedData,
    )

    leaf = await store.get_leaf(ctx.thread_id)
    thread = await store.get_thread(ctx.thread_id)
    memory_event = Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=leaf.event_id if leaf else None,
        thread_id=ctx.thread_id,
        session_id=thread.session_id,
        type=EventType.MEMORY_RETRIEVED,
        data=MemoryRetrievedData(
            scope="session",
            query={"semantic_query": "prior context"},
            results=[
                {
                    "content": {"note": "User prefers concise replies."},
                    "tags": ["preferences"],
                },
            ],
            variant="recent_buffer",
        ).model_dump(mode="json"),
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )
    await store.append_event(ctx.thread_id, memory_event)

    await engine.execute(_config(), ctx)

    seen_messages = _as_messages(fake.calls[0].context["messages"])

    # The memory retrieval surfaces as a system message in the
    # projection. Find it and verify the verbatim content survives.
    system_msgs = [m for m in seen_messages if m["role"] == "system"]
    # The first system message is the agent's prompt; the second is
    # the memory injection (assembled via _project_events).
    memory_msg = next(
        (m for m in system_msgs if "<memory>" in m["content"]),
        None,
    )
    assert memory_msg is not None
    # Verbatim — the {"note": ...} dict appears in the projection
    # under the deterministic json.dumps form.
    assert "User prefers concise replies." in memory_msg["content"]

    # §B.6 amendment also holds: the captured model.request.messages
    # has the same memory injection (load-bearing for replay).
    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    request_events = [e for e in events if e.type == EventType.MODEL_REQUEST]
    captured_messages = request_events[0].data["messages"]
    captured_memory_msg = next(
        (
            m
            for m in captured_messages
            if m["role"] == "system" and "<memory>" in m["content"]
        ),
        None,
    )
    assert captured_memory_msg is not None
    assert "User prefers concise replies." in captured_memory_msg["content"]


# ===========================================================================
# TC-ENG-04 — Diagnostic error does not end exchange
# ===========================================================================


class _BoomArgs(BaseModel):
    pass


@tool(
    name="boom",
    description="Always fails.",
    args_schema=_BoomArgs,
)
async def _boom_tool(_args: _BoomArgs, _ctx: ToolContext) -> dict[str, Any]:
    from errors import ToolExecutionFailed

    raise ToolExecutionFailed(
        tool_name="boom",
        error_class="ScriptedExplosion",
    )


async def test_tc_eng_04_diagnostic_error_continues_exchange(tmp_path: Path) -> None:
    fake = FakeLLM(
        script=[
            ToolCallResponse(tool="boom", args={}),
            TextResponse(text="The tool failed; I'll skip it."),
        ],
    )
    engine, store, _am = await _build_engine(
        tmp_path,
        fake,
        extra_tools=[_boom_tool],
    )
    ctx = await _new_session_context(store)

    # Should NOT raise — tool failure is Diagnostic per spec §1.6.
    result = await engine.execute(_config_with_tool(), ctx)
    assert "tool failed" in result.data["content"]["text"].lower()

    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    types = {e.type for e in events}
    assert EventType.TOOL_FAILED in types
    assert EventType.AGENT_MESSAGE in types
    # Critical: no exchange_failed.
    assert EventType.THREAD_EXCHANGE_FAILED not in types


# ===========================================================================
# TC-ENG-05 / TC-ENG-06 — Fatal error semantics
# ===========================================================================


from tests.fixtures.fake_llm import ErrorResponse  # noqa: E402


async def test_tc_eng_05_fatal_error_raises_sync(tmp_path: Path) -> None:
    # Use a Recoverable error multiple times so the engine's retry
    # exhausts and Fatals out as model.retry_exhausted.
    fake = FakeLLM(
        script=[
            ErrorResponse(error_type="model.upstream_error", message="upstream down"),
            ErrorResponse(error_type="model.upstream_error", message="still down"),
            ErrorResponse(error_type="model.upstream_error", message="and again"),
        ],
    )
    engine, store, _am = await _build_engine(tmp_path, fake)
    ctx = await _new_session_context(store)
    cfg = _config().model_copy(
        update={"runtime": RuntimeSpec(max_retries=2, timeout_seconds=None)},
    )

    with pytest.raises(AgentExecutionError) as exc_info:
        await engine.execute(cfg, ctx)

    failed_event = exc_info.value.failed_event
    assert failed_event.type == EventType.THREAD_EXCHANGE_FAILED
    # The catalog code is `model.retry_exhausted` (Fatal).
    assert failed_event.data["error"]["type"] == "model.retry_exhausted"


async def test_tc_eng_06_fatal_error_ends_stream(tmp_path: Path) -> None:
    fake = FakeLLM(
        script=[
            ErrorResponse(error_type="model.upstream_error", message="x"),
            ErrorResponse(error_type="model.upstream_error", message="x"),
            ErrorResponse(error_type="model.upstream_error", message="x"),
        ],
    )
    engine, store, _am = await _build_engine(tmp_path, fake)
    ctx = await _new_session_context(store)
    cfg = _config().model_copy(
        update={"runtime": RuntimeSpec(max_retries=2, timeout_seconds=None)},
    )

    events: list[Event] = []
    async for event in engine.execute_stream(cfg, ctx):
        events.append(event)

    # The stream ended cleanly; no exception. The last event is failed.
    assert events[-1].type == EventType.THREAD_EXCHANGE_FAILED
    assert events[-1].data["error"]["type"] == "model.retry_exhausted"


# ===========================================================================
# TC-ENG-07 — Authorization denial at pre-flight
# ===========================================================================


async def test_tc_eng_07_authz_denial(tmp_path: Path) -> None:
    fake = FakeLLM(script=[])  # never called
    engine, store, _am = await _build_engine(
        tmp_path,
        fake,
        authz_rules=[
            {"who": "user.local", "what": r"agent\.execute\..*", "effect": "deny"},
        ],
    )
    ctx = await _new_session_context(store)

    with pytest.raises(AgentExecutionError) as exc_info:
        await engine.execute(_config(), ctx)

    failed_event = exc_info.value.failed_event
    assert failed_event.type == EventType.THREAD_EXCHANGE_FAILED
    assert failed_event.data["error"]["type"] == "agent.unauthorized"

    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    types = {e.type for e in events}
    # No model.request, no tool events — denied at pre-flight.
    assert EventType.MODEL_REQUEST not in types
    assert EventType.TOOL_CALLED not in types
    # No exchange_started either — authz denial happens at step 3,
    # before step 8.
    assert EventType.THREAD_EXCHANGE_STARTED not in types
    # But the failed event IS in the log.
    assert EventType.THREAD_EXCHANGE_FAILED in types


# ===========================================================================
# TC-ENG-08 — Statelessness across calls
# ===========================================================================


async def test_tc_eng_08_statelessness_across_restart(tmp_path: Path) -> None:
    fake1 = FakeLLM(script=[TextResponse(text="Turn 1 reply.")])
    engine1, store, _am = await _build_engine(tmp_path, fake1)
    ctx = await _new_session_context(store)

    await engine1.execute(_config(), ctx)

    # Simulate restart: build a fresh engine + a fresh fake LLM,
    # reusing the same store + thread.
    fake2 = FakeLLM(script=[TextResponse(text="Turn 2 reply.")])
    engine2, _store2, _am2 = await _build_engine(tmp_path, fake2)

    # New trigger on the same thread.
    ctx2 = ExecutionContext(
        thread_id=ctx.thread_id,
        trigger={"kind": "user_message", "content": "Follow-up?"},
    )
    result2 = await engine2.execute(_config(), ctx2)
    assert "Turn 2" in result2.data["content"]["text"]

    # Turn 2's LLM call saw Turn 1's history (assistant turn from
    # the previous exchange + new user message + system prompt).
    seen = _as_messages(fake2.calls[0].context["messages"])
    roles = [m["role"] for m in seen]
    # system (always), then user(turn1), assistant(turn1), user(turn2)
    assert roles[0] == "system"
    user_count = sum(1 for r in roles if r == "user")
    assistant_count = sum(1 for r in roles if r == "assistant")
    assert user_count == 2  # turn 1 and turn 2 user messages
    assert assistant_count == 1  # turn 1 agent message


# ===========================================================================
# TC-ENG-09 — Cancellation mid-exchange
# ===========================================================================


async def test_tc_eng_09_cancellation_mid_exchange(tmp_path: Path) -> None:
    cancel = CancelToken()

    # The LLM script will return a tool call so we have at least
    # two iterations; we cancel between them by inserting a hook.
    fake = FakeLLM(
        script=[
            ToolCallResponse(tool="ping", args={"message": "a"}),
            TextResponse(text="should not get here"),
        ],
    )
    engine, store, _am = await _build_engine(
        tmp_path,
        fake,
        extra_tools=[_ping_tool],
    )
    ctx = ExecutionContext(
        thread_id=(await _new_session_context(store)).thread_id,
        trigger={"kind": "user_message", "content": "Hi."},
        cancellation=cancel,
    )

    async def cancel_after_first_tool() -> None:
        # Wait until at least the first tool.called event lands, then
        # cancel. The framework will check is_cancelled at the next
        # iteration boundary.
        for _ in range(50):
            events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
            if any(e.type == EventType.TOOL_RESULT for e in events):
                cancel.cancel()
                return
            await asyncio.sleep(0.01)

    canceller = asyncio.create_task(cancel_after_first_tool())

    events: list[Event] = []
    async for event in engine.execute_stream(_config_with_tool(), ctx):
        events.append(event)

    await canceller

    # The stream ended; either with thread.exchange_failed
    # (exchange.cancelled) or with the normal completion if
    # cancellation came after the framework already produced a final
    # answer. The test requires the cancelled error code when failed.
    final = events[-1]
    if final.type == EventType.THREAD_EXCHANGE_FAILED:
        assert final.data["error"]["type"] == "exchange.cancelled"


# ===========================================================================
# TC-ENG-10 — Force-invoke emits synthetic read
# ===========================================================================


def _write_skill(root: Path, bundle: str, name: str, body: str) -> Path:
    bundle_dir = root / "bundles" / bundle
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "bundle.yaml").write_text(
        yaml.safe_dump({"name": bundle, "version": "0.1", "description": "x"}),
        encoding="utf-8",
    )
    skill_dir = bundle_dir / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    md = skill_dir / "SKILL.md"
    md.write_text(
        f"---\nname: {name}\ndescription: A test skill.\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return md


async def test_tc_eng_10_force_invoke_emits_synthetic_read(tmp_path: Path) -> None:
    _platform_yaml(tmp_path)
    md_path = _write_skill(tmp_path, "acme", "literature", "Skill body content.")

    fake = FakeLLM(script=[TextResponse(text="Done.")])
    engine, store, _am = await _build_engine(tmp_path, fake)

    cfg = _config().model_copy(
        update={
            "subsystems": {
                "tools": SubsystemConfig(
                    variant="standard",
                    config={"builtin": ["read"]},
                ),
                "skills": SubsystemConfig(
                    variant="standard",
                    config={"enabled": [{"ref": "acme/skills/literature"}]},
                ),
            },
        },
    )

    session = await store.create_session("ag_test", name="force-invoke-session")
    threads = await store.list_threads(session.session_id)
    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={
            "kind": "user_message_with_force_skill",
            "content": "Use the skill.",
            "force_skills": ["acme/skills/literature"],
        },
    )

    await engine.execute(cfg, ctx)

    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    # The synthetic tool.called / tool.result appears BEFORE
    # thread.exchange_started (spec §2.1 step 7 vs step 8).
    synthetic_calls = [
        e
        for e in events
        if e.type == EventType.TOOL_CALLED and e.actor.kind == "system"
    ]
    assert len(synthetic_calls) == 1
    assert synthetic_calls[0].data["arguments"]["path"] == str(md_path)

    # The synthetic read precedes thread.exchange_started.
    types_in_order = [e.type for e in events]
    started_idx = types_in_order.index(EventType.THREAD_EXCHANGE_STARTED)
    synthetic_called_idx = next(
        i
        for i, e in enumerate(events)
        if e.type == EventType.TOOL_CALLED and e.actor.kind == "system"
    )
    assert synthetic_called_idx < started_idx


# ===========================================================================
# TC-ENG-11 — Skills listing appended to system prompt
# ===========================================================================


async def test_tc_eng_11_skills_listing_in_system_prompt(tmp_path: Path) -> None:
    """qol-skills-jinja-variable-symmetric-with-tools — the
    pre-QoL post-append at ``assembly.py:83-87`` is gone. The
    rendered prompt's ``{{ skills }}`` block is now the
    in-prompt skills surface. Sk-I2 (disable-model-invocation
    skips the skill) is enforced at the engine's SkillsVar
    construction so the same assertions hold."""
    _platform_yaml(tmp_path)
    _write_skill(tmp_path, "acme", "alpha", "alpha body")
    _write_skill(tmp_path, "acme", "beta", "beta body")
    # Mark `beta` as disable-model-invocation by re-writing SKILL.md.
    beta_md = tmp_path / "bundles" / "acme" / "skills" / "beta" / "SKILL.md"
    beta_md.write_text(
        "---\n"
        "name: beta\n"
        "description: A hidden skill.\n"
        "disable-model-invocation: true\n"
        "---\n\nbeta body\n",
        encoding="utf-8",
    )

    fake = FakeLLM(script=[TextResponse(text="Done.")])
    engine, store, _am = await _build_engine(tmp_path, fake)

    cfg = _config().model_copy(
        update={
            # Prompt template renders the skills block — the
            # auto-append is gone; template owns placement now.
            "prompt": (
                "You are a helpful test agent.\n\n## Available skills\n\n{{ skills }}"
            ),
            "subsystems": {
                "skills": SubsystemConfig(
                    variant="standard",
                    config={
                        "enabled": [
                            {"ref": "acme/skills/alpha"},
                            {"ref": "acme/skills/beta"},
                        ],
                    },
                ),
            },
        },
    )

    ctx = await _new_session_context(store)
    await engine.execute(cfg, ctx)

    system_prompt = str(fake.calls[0].context["system_prompt"])
    assert "alpha" in system_prompt
    assert "beta" not in system_prompt  # Sk-I2 — hidden skill excluded.
