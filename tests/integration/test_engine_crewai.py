"""TC-ENG-01..11 against :class:`CrewAIFramework` — integration tier.

The same case bodies live as component tests against FakeFramework
(tests/component/test_engine.py). This file re-runs them with the
production framework so divergences from the FakeFramework view land
as reviewable test failures rather than silent wrap-time drift.

Golden files: separate per framework
(``tests/golden/tc_eng_01_simple_crewai.jsonl`` vs
``tests/golden/tc_eng_01_simple.jsonl``). The diff between the two is
the framework's documented contribution to ``model.request.data.messages``
— CrewAI's prompt-template injection (role/goal/backstory wrap, task
description preamble, expected_output trailer). The §B.6 amendment
captures this verbatim; replay reads from the captured event regardless.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import BaseModel

from engine import (
    AgentExecutionError,
    CrewAIFramework,
    ExecutionContext,
)
from schemas import (
    AgentId,
    EventType,
    IOSpec,
    ModelSpec,
    ResolvedAgentConfig,
    RuntimeSpec,
    SubsystemConfig,
)
from tests.fixtures.engine_builder import (
    build_engine,
    new_session_context,
)
from tests.fixtures.fake_llm import (
    ErrorResponse,
    FakeLLM,
    TextResponse,
    ToolCallResponse,
)
from tests.golden._helpers import assert_matches_golden
from tools import ToolContext, tool

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Config helpers (same shape as the component tests)
# ---------------------------------------------------------------------------


def _config() -> ResolvedAgentConfig:
    return ResolvedAgentConfig(
        schema_version="0.1",
        assembly_version="1",
        agent_id=AgentId("ag_test"),
        name="test-agent",
        description="A test agent.",
        model=ModelSpec(provider="fake", model="fake-model"),
        prompt="You are a helpful test agent.",
        io=IOSpec(mode="conversational"),
    )


def _config_with_tool() -> ResolvedAgentConfig:
    return _config().model_copy(
        update={
            "subsystems": {
                "tools": SubsystemConfig(
                    variant="standard",
                    config={"builtin": []},
                ),
            },
        },
    )


class _PingArgs(BaseModel):
    message: str


@tool(name="ping", description="Echo a message.", args_schema=_PingArgs)
async def _ping_tool(
    args: _PingArgs,
    _ctx: ToolContext,
) -> dict[str, Any]:
    return {"echoed": args.message}


# ===========================================================================
# TC-ENG-01 against CrewAIFramework
# ===========================================================================


async def test_tc_eng_01_simple_against_crewai(tmp_path: Path) -> None:
    fake = FakeLLM(script=[TextResponse(text="Hi from CrewAI.")])
    engine, store, _am = await build_engine(
        tmp_path,
        fake,
        framework=CrewAIFramework(),
    )
    ctx = await new_session_context(store)

    result = await engine.execute(_config(), ctx)
    assert result.type == EventType.AGENT_MESSAGE
    assert "Hi from CrewAI" in result.data["content"]["text"]

    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    engine_events = [e for e in events if e.type != EventType.SESSION_OPENED]

    # Event-type sequence — must match FakeFramework's run exactly
    # (this is the framework-agnostic invariant: same engine, same
    # event taxonomy, same order).
    expected_types = (
        EventType.CONFIG_SET,
        EventType.THREAD_EXCHANGE_STARTED,
        EventType.USER_MESSAGE,
        EventType.MODEL_REQUEST,
        EventType.MODEL_RESPONSE,
        EventType.AGENT_MESSAGE,
        EventType.THREAD_EXCHANGE_COMPLETED,
    )
    assert tuple(e.type for e in engine_events) == expected_types

    # CrewAI-specific golden — captures the framework wrap on
    # model.request.data.messages.
    assert_matches_golden("tc_eng_01_simple_crewai", engine_events)


# ===========================================================================
# TC-ENG-02 against CrewAIFramework
# ===========================================================================


async def test_tc_eng_02_tool_call_against_crewai(tmp_path: Path) -> None:
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
    engine, store, _am = await build_engine(
        tmp_path,
        fake,
        framework=CrewAIFramework(),
        extra_tools=[_ping_tool],
    )
    ctx = await new_session_context(store)

    result = await engine.execute(_config_with_tool(), ctx)
    assert "ack" in result.data["content"]["text"]

    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    engine_events = [e for e in events if e.type != EventType.SESSION_OPENED]

    expected_types = (
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
    assert tuple(e.type for e in engine_events) == expected_types

    assert_matches_golden("tc_eng_02_tool_call_crewai", engine_events)


# ===========================================================================
# TC-ENG-04 — Diagnostic tool failure continues exchange
# ===========================================================================


class _BoomArgs(BaseModel):
    pass


@tool(name="boom", description="Always fails.", args_schema=_BoomArgs)
async def _boom_tool(_args: _BoomArgs, _ctx: ToolContext) -> dict[str, Any]:
    from errors import ToolExecutionFailed  # noqa: PLC0415

    raise ToolExecutionFailed(
        tool_name="boom",
        error_class="ScriptedExplosion",
    )


async def test_tc_eng_04_diagnostic_continues_against_crewai(
    tmp_path: Path,
) -> None:
    fake = FakeLLM(
        script=[
            ToolCallResponse(tool="boom", args={}, tool_call_id="call_boom"),
            TextResponse(text="The tool failed; I'll skip it."),
        ],
    )
    engine, store, _am = await build_engine(
        tmp_path,
        fake,
        framework=CrewAIFramework(),
        extra_tools=[_boom_tool],
    )
    ctx = await new_session_context(store)

    result = await engine.execute(_config_with_tool(), ctx)
    assert "tool failed" in result.data["content"]["text"].lower()

    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    types = {e.type for e in events}
    assert EventType.TOOL_FAILED in types
    assert EventType.AGENT_MESSAGE in types
    assert EventType.THREAD_EXCHANGE_FAILED not in types


# ===========================================================================
# TC-ENG-05 — Fatal sync raise against CrewAIFramework
# ===========================================================================


async def test_tc_eng_05_fatal_raises_against_crewai(tmp_path: Path) -> None:
    fake = FakeLLM(
        script=[
            ErrorResponse(
                error_type="model.upstream_error",
                message="upstream down",
            ),
            ErrorResponse(
                error_type="model.upstream_error",
                message="still down",
            ),
            ErrorResponse(
                error_type="model.upstream_error",
                message="and again",
            ),
        ],
    )
    engine, store, _am = await build_engine(
        tmp_path,
        fake,
        framework=CrewAIFramework(),
    )
    ctx = await new_session_context(store)
    cfg = _config().model_copy(
        update={"runtime": RuntimeSpec(max_retries=2, timeout_seconds=None)},
    )

    with pytest.raises(AgentExecutionError) as exc_info:
        await engine.execute(cfg, ctx)

    failed_event = exc_info.value.failed_event
    assert failed_event.type == EventType.THREAD_EXCHANGE_FAILED
    assert failed_event.data["error"]["type"] == "model.retry_exhausted"


# ===========================================================================
# TC-ENG-07 — Authorization denial at pre-flight
# ===========================================================================


async def test_tc_eng_07_authz_denial_against_crewai(tmp_path: Path) -> None:
    fake = FakeLLM(script=[])  # never called
    engine, store, _am = await build_engine(
        tmp_path,
        fake,
        framework=CrewAIFramework(),
        authz_rules=[
            {"who": "user.local", "what": r"agent\.execute\..*", "effect": "deny"},
        ],
    )
    ctx = await new_session_context(store)

    with pytest.raises(AgentExecutionError) as exc_info:
        await engine.execute(_config(), ctx)

    failed_event = exc_info.value.failed_event
    assert failed_event.data["error"]["type"] == "agent.unauthorized"

    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    types = {e.type for e in events}
    # Pre-flight denial — no model.request, no tool events.
    assert EventType.MODEL_REQUEST not in types
    assert EventType.TOOL_CALLED not in types


# ===========================================================================
# TC-ENG-08 — Statelessness across restart (CrewAI)
# ===========================================================================


async def test_tc_eng_08_statelessness_against_crewai(tmp_path: Path) -> None:
    fake1 = FakeLLM(script=[TextResponse(text="Turn 1 reply.")])
    engine1, store, _am = await build_engine(
        tmp_path,
        fake1,
        framework=CrewAIFramework(),
    )
    ctx = await new_session_context(store)
    await engine1.execute(_config(), ctx)

    # Restart: fresh engine + fresh fake; same store + same thread.
    fake2 = FakeLLM(script=[TextResponse(text="Turn 2 reply.")])
    engine2, _store2, _am2 = await build_engine(
        tmp_path,
        fake2,
        framework=CrewAIFramework(),
    )
    ctx2 = ExecutionContext(
        thread_id=ctx.thread_id,
        trigger={"kind": "user_message", "content": "Follow-up?"},
    )
    result2 = await engine2.execute(_config(), ctx2)
    assert "Turn 2" in result2.data["content"]["text"]


# ===========================================================================
# TC-ENG-11 — Skills listing surfaces in CrewAI's wrapped prompt
# ===========================================================================


import yaml  # noqa: E402


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


async def test_tc_eng_11_skills_listing_against_crewai(tmp_path: Path) -> None:
    from tests.fixtures.engine_builder import write_platform_yaml  # noqa: PLC0415

    write_platform_yaml(tmp_path)
    _write_skill(tmp_path, "acme", "alpha", "alpha body")
    _write_skill(tmp_path, "acme", "beta", "beta body")
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
    engine, store, _am = await build_engine(
        tmp_path,
        fake,
        framework=CrewAIFramework(),
    )

    cfg = _config().model_copy(
        update={
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
    ctx = await new_session_context(store)
    await engine.execute(cfg, ctx)

    # CrewAI builds a wrapped system prompt; the skills listing lives
    # inside our backstory which becomes part of the wrap.
    seen = fake.calls[0].context["messages"]
    assert isinstance(seen, list)
    system_text = next(
        (
            str(m["content"])
            for m in seen
            if isinstance(m, dict) and m.get("role") == "system"
        ),
        "",
    )
    assert "alpha" in system_text
    # Sk-I2 — disabled-from-model skill stays out of the listing
    # regardless of which framework wraps the prompt.
    assert "beta" not in system_text


# ===========================================================================
# TC-ENG-06 — Fatal error ends stream cleanly against CrewAIFramework
# ===========================================================================


async def test_tc_eng_06_fatal_ends_stream_against_crewai(tmp_path: Path) -> None:
    """Streaming counterpart to TC-ENG-05. The engine MUST NOT raise
    from execute_stream on a Fatal; the failed event is the final
    yielded item and the iterator ends."""
    from schemas import Event  # noqa: PLC0415

    fake = FakeLLM(
        script=[
            ErrorResponse(error_type="model.upstream_error", message="x"),
            ErrorResponse(error_type="model.upstream_error", message="x"),
            ErrorResponse(error_type="model.upstream_error", message="x"),
        ],
    )
    engine, store, _am = await build_engine(
        tmp_path,
        fake,
        framework=CrewAIFramework(),
    )
    ctx = await new_session_context(store)
    cfg = _config().model_copy(
        update={"runtime": RuntimeSpec(max_retries=2, timeout_seconds=None)},
    )

    events: list[Event] = []
    async for event in engine.execute_stream(cfg, ctx):
        events.append(event)

    # Stream ended without raising; last event is the Fatal.
    assert events[-1].type == EventType.THREAD_EXCHANGE_FAILED
    assert events[-1].data["error"]["type"] == "model.retry_exhausted"


# ===========================================================================
# TC-ENG-09 — Cancellation mid-exchange across the loop-bridge
# ===========================================================================


async def test_tc_eng_09_cancellation_against_crewai(tmp_path: Path) -> None:
    """Cancellation across CrewAI's worker-thread → main-loop bridge.

    Step 12b review #4 flagged the bridge's blocking behavior:
    ``asyncio.run_coroutine_threadsafe(...).result()`` blocks the
    worker thread until the LLMHandle returns. Cancellation cooperation
    therefore lives at iteration boundaries, not mid-call. This test
    pins that boundary: cancel BEFORE the framework runs, then assert
    the typed cancellation surfaces via the engine's catalog (not as
    a raw RuntimeError).

    Mid-call cancellation is documented as a known limitation per
    the Step 12b review and the CrewAIFramework module docstring."""
    import asyncio  # noqa: PLC0415

    from engine import CancelToken  # noqa: PLC0415
    from schemas import Event  # noqa: PLC0415

    cancel = CancelToken()
    cancel.cancel()  # pre-cancel; framework's first cancellation poll fires.

    fake = FakeLLM(script=[TextResponse(text="should not be reached")])
    engine, store, _am = await build_engine(
        tmp_path,
        fake,
        framework=CrewAIFramework(),
    )
    base_ctx = await new_session_context(store)
    ctx = ExecutionContext(
        thread_id=base_ctx.thread_id,
        trigger=base_ctx.trigger,
        cancellation=cancel,
    )

    events: list[Event] = []
    async for event in engine.execute_stream(_config(), ctx):
        events.append(event)

    # The stream ended; the final event is thread.exchange_failed
    # with the catalog-typed exchange.cancelled (verifying review #5
    # fix — raw RuntimeError would have surfaced as
    # platform.internal_error).
    assert events[-1].type == EventType.THREAD_EXCHANGE_FAILED
    assert events[-1].data["error"]["type"] == "exchange.cancelled"
    # FakeLLM was never reached (we pre-cancelled).
    assert len(fake.calls) == 0
    # asyncio import preserved across PLR — runtime use here is to make
    # the helper-import block clear; the engine's CancelToken sets the
    # underlying asyncio.Event itself.
    del asyncio


# ===========================================================================
# TC-ENG-10 — Force-invoke synthetic read sits OUTSIDE CrewAI's loop
# ===========================================================================


def _write_skill_for_force_invoke(
    root: Path,
    body: str = "Skill body content.",
) -> Path:
    """Helper: create acme/skills/literature with the given body."""
    return _write_skill(root, "acme", "literature", body)


async def test_tc_eng_10_force_invoke_against_crewai(tmp_path: Path) -> None:
    """Pre-flight step 7 emits the synthetic read BEFORE
    Framework.run_exchange is even called. CrewAI must not see /
    interfere with the synthetic tool.called/tool.result pair. This
    test verifies the engine's pre-flight isolation holds: the
    synthetic events are in the log, principal=system, and they
    appear strictly before the model.request from CrewAI's wrap."""
    from tests.fixtures.engine_builder import write_platform_yaml  # noqa: PLC0415

    write_platform_yaml(tmp_path)
    md_path = _write_skill_for_force_invoke(tmp_path)

    fake = FakeLLM(script=[TextResponse(text="Done.")])
    engine, store, _am = await build_engine(
        tmp_path,
        fake,
        framework=CrewAIFramework(),
    )

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

    session = await store.create_session("ag_test", name="force-invoke")
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
    # The synthetic read landed exactly once, with actor.kind=system
    # and the SKILL.md path on the arguments.
    synthetic_called = [
        e
        for e in events
        if e.type == EventType.TOOL_CALLED and e.actor.kind == "system"
    ]
    assert len(synthetic_called) == 1
    assert synthetic_called[0].data["arguments"]["path"] == str(md_path)

    # Critical: the synthetic call precedes BOTH thread.exchange_started
    # and the CrewAI-issued model.request — i.e. CrewAI never saw it.
    types_in_order = [e.type for e in events]
    synthetic_idx = next(
        i
        for i, e in enumerate(events)
        if e.type == EventType.TOOL_CALLED and e.actor.kind == "system"
    )
    started_idx = types_in_order.index(EventType.THREAD_EXCHANGE_STARTED)
    model_request_idx = types_in_order.index(EventType.MODEL_REQUEST)
    assert synthetic_idx < started_idx < model_request_idx
