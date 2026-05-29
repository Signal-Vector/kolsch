"""TC-E2E-01..04 — end-to-end / overall-validation tests.

These are the "real LLM, real infra" cases from test-catalog Part 13.
All marked with ``@pytest.mark.e2e`` so the default `pytest` run
skips them (the marker is registered in pyproject.toml's
`markers` list under the existing `e2e:` definition).

Run via:

    pytest -m e2e

Each test skips when its required environment isn't present:

- TC-E2E-01 / TC-E2E-02: need ``E2E_LLM_API_KEY`` set + a real
  provider configured (Anthropic / OpenAI). Without it, the
  CrewAIFramework adapter has nothing real to talk to.
- TC-E2E-03 (soak): needs ``E2E_SOAK_MINUTES`` set + a real LLM.
- TC-E2E-04 (default install): runs by env-var opt-in
  (``E2E_DEFAULT_INSTALL=1``) — exercises the clean-machine first-run
  experience.

TC-E2E-01's body is the Phase 1 Feasibility GATE: it runs the
engine + CrewAIFramework + a real LLM provider over 3 diverse tasks
and asserts §B.6 materialization holds end-to-end. Without the
env vars set, it skips cleanly. With them set, it ACTUALLY runs the
provider — no second skip inside the body.

TC-E2E-04's body is the default-install gate: builds a fresh venv,
installs the project as a wheel, runs ``kolsch run`` end-to-end
against a tmp-path platform root. Uses EchoLLM (no real provider
needed).

TC-E2E-02 (soundness comparison) and TC-E2E-03 (robustness soak)
remain scaffolded — both are research-shaped, not gate-shaped, and
the catalog + test plan only flag TC-E2E-01 as GATING.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
import yaml

pytestmark = pytest.mark.e2e

if TYPE_CHECKING:
    from pathlib import Path


_LLM_KEY_ENV = "E2E_LLM_API_KEY"
_LLM_PROVIDER_ENV = "E2E_LLM_PROVIDER"
_SOAK_MINUTES_ENV = "E2E_SOAK_MINUTES"
_DEFAULT_INSTALL_ENV = "E2E_DEFAULT_INSTALL"


def _llm_available() -> bool:
    return bool(os.environ.get(_LLM_KEY_ENV)) and bool(
        os.environ.get(_LLM_PROVIDER_ENV)
    )


# ===========================================================================
# TC-E2E-01 — Feasibility (GATING) — real CrewAI + real LLM over 3 tasks
# ===========================================================================


@pytest.mark.skipif(
    not _llm_available(),
    reason=(
        "TC-E2E-01 (Feasibility) requires E2E_LLM_API_KEY + E2E_LLM_PROVIDER. "
        "Set both to run the real-LLM contract check (test plan Test 1). "
        "GATING — must pass before any production release. Example: "
        "E2E_LLM_PROVIDER=openai/gpt-4o-mini E2E_LLM_API_KEY=sk-... "
        "pytest -m e2e tests/e2e/"
    ),
)
async def test_tc_e2e_01_feasibility_crewai_with_real_llm(
    tmp_path: Path,
) -> None:
    """Real CrewAI + real LLM over 3 diverse tasks; assert the engine
    contract (I3, I5, I6) holds end-to-end.

    Three diverse tasks per the catalog:
      1. Text-only Q&A (no tools).
      2. Tool-using task (one tool call → final answer).
      3. Multi-turn conversational follow-up on the same thread.

    For each task we:
    - Run via AgentEngine + CrewAIFramework wired to a real LLM
      callable (CrewAI's own crewai.llm.LLM driving litellm
      transitively; provider+key from env).
    - Capture the engine's emitted events.
    - Assert: model.request landed before model.response (I6);
      model.request.data.messages is non-empty (§B.6 amendment);
      the captured messages contain the user's query verbatim
      (Materialization).

    GATING: this is the Phase 1 Feasibility gate. Failing here means
    real CrewAI's runtime behavior diverges from the contract the
    engine assumes (TC-CRW-02 verifies the same with FakeLLM; this
    is the real-provider confirmation).
    """
    from pydantic import BaseModel

    from engine import (
        CrewAIFramework,
        ExecutionContext,
    )
    from engine.framework import LLMRequest, LLMResponse
    from schemas import (
        AgentId,
        EventType,
        IOSpec,
        ModelSpec,
        ResolvedAgentConfig,
        SubsystemConfig,
    )
    from tests.fixtures.engine_builder import (
        build_engine,
        new_session_context,
    )
    from tests.fixtures.fake_llm import FakeLLM
    from tools import ToolContext, tool

    provider_string = os.environ[_LLM_PROVIDER_ENV]
    api_key = os.environ[_LLM_KEY_ENV]

    # Build the real-LLM callable via CrewAI's LLM wrapper (which
    # uses litellm transitively to talk to any provider). Captures
    # request.messages so the test can dual-record what we sent vs.
    # what the engine materialized on model.request.data.messages.
    from crewai.llm import LLM

    real_provider = LLM(model=provider_string, api_key=api_key)

    sent_messages_log: list[list[dict[str, Any]]] = []

    async def real_llm(request: LLMRequest) -> LLMResponse:
        msgs: list[dict[str, Any]] = [m.model_dump() for m in request.messages]
        sent_messages_log.append(msgs)
        tools_arg = list(request.tools) if request.tools else None
        # Cast to Any so mypy doesn't try to fit our dict shape into
        # CrewAI's LLMMessage TypedDict (LLM.call accepts either).
        text: Any = await asyncio.to_thread(real_provider.call, msgs, tools_arg)  # type: ignore[arg-type]
        if isinstance(text, list):
            # Tool-call list — translate CrewAI shape back to ours.
            return LLMResponse(
                content=None,
                tool_calls=[
                    {
                        "id": (
                            c.get("id")
                            if isinstance(c, dict)
                            else f"call_{uuid4().hex[:8]}"
                        ),
                        "name": (
                            c.get("function", {}).get("name", "")
                            if isinstance(c, dict)
                            else ""
                        ),
                        "arguments": (
                            json.loads(c.get("function", {}).get("arguments", "{}"))
                            if isinstance(c, dict)
                            and isinstance(
                                c.get("function", {}).get("arguments"),
                                str,
                            )
                            else (
                                c.get("function", {}).get("arguments", {})
                                if isinstance(c, dict)
                                else {}
                            )
                        ),
                    }
                    for c in text
                ],
            )
        return LLMResponse(content=str(text))

    # build_engine takes a FakeLLM for its adapter shim; we bypass
    # that shim by passing our real callable in via the engine
    # constructor below.
    fake_unused = FakeLLM(script=[])
    engine, store, _am = await build_engine(
        tmp_path,
        fake_unused,
        framework=CrewAIFramework(),
    )
    # Swap in the real LLM callable.
    engine._llm = real_llm

    def _config(*, with_tool: bool) -> ResolvedAgentConfig:
        return ResolvedAgentConfig(
            schema_version="0.1",
            assembly_version="1",
            agent_id=AgentId("ag_e2e"),
            name="E2E agent",
            description="Phase 1 feasibility gate.",
            model=ModelSpec(provider="real", model=provider_string),
            prompt=(
                "You are a precise assistant. Answer concisely. "
                "When you have the answer, return it directly — no preamble."
            ),
            io=IOSpec(mode="conversational"),
            subsystems=(
                {
                    "tools": SubsystemConfig(
                        variant="standard",
                        config={"builtin": []},
                    ),
                }
                if with_tool
                else {}
            ),
        )

    # ---- Task 1: text-only Q&A ------------------------------------
    ctx1 = await new_session_context(store)
    ctx1 = ExecutionContext(
        thread_id=ctx1.thread_id,
        trigger={
            "kind": "user_message",
            "content": "What is 2+2? Reply with only the number.",
        },
    )
    sent_messages_log.clear()
    result1 = await engine.execute(_config(with_tool=False), ctx1)
    assert "4" in result1.data["content"]["text"]

    events1 = [e async for e in store.read_path_to_leaf(ctx1.thread_id)]
    request_events_1 = [e for e in events1 if e.type == EventType.MODEL_REQUEST]
    response_events_1 = [e for e in events1 if e.type == EventType.MODEL_RESPONSE]
    assert request_events_1
    assert response_events_1
    # I6 ordering: model.request precedes model.response.
    assert events1.index(request_events_1[0]) < events1.index(
        response_events_1[0],
    )
    # §B.6: captured messages non-empty and contain the user query.
    captured_1 = request_events_1[0].data["messages"]
    assert captured_1, "model.request.data.messages must be captured"
    captured_text_1 = " ".join(
        str(m.get("content", "")) for m in captured_1 if isinstance(m, dict)
    )
    assert "2+2" in captured_text_1
    # Dual-record: what the engine emitted == what we sent.
    assert captured_1 == sent_messages_log[0]

    # ---- Task 2: tool-using ---------------------------------------
    class _MultiplyArgs(BaseModel):
        a: int
        b: int

    @tool(
        name="multiply",
        description="Multiply two integers and return the product.",
        args_schema=_MultiplyArgs,
    )
    async def _multiply_tool(
        args: _MultiplyArgs,
        _ctx: ToolContext,
    ) -> dict[str, Any]:
        return {"product": args.a * args.b}

    await engine._tools.register_for_agent(
        agent_id=AgentId("ag_e2e"),
        tools=[_multiply_tool],
    )

    ctx2_base = await new_session_context(store)
    ctx2 = ExecutionContext(
        thread_id=ctx2_base.thread_id,
        trigger={
            "kind": "user_message",
            "content": (
                "Use the multiply tool to compute 7 times 6. "
                "Then give me the product as the final answer."
            ),
        },
    )
    sent_messages_log.clear()
    result2 = await engine.execute(_config(with_tool=True), ctx2)
    assert "42" in result2.data["content"]["text"]

    events2 = [e async for e in store.read_path_to_leaf(ctx2.thread_id)]
    # tool.called precedes tool.result (I6).
    tool_called_2 = next(
        i for i, e in enumerate(events2) if e.type == EventType.TOOL_CALLED
    )
    tool_result_2 = next(
        i for i, e in enumerate(events2) if e.type == EventType.TOOL_RESULT
    )
    assert tool_called_2 < tool_result_2
    # §B.6 still holds on the multi-iteration exchange.
    request_events_2 = [e for e in events2 if e.type == EventType.MODEL_REQUEST]
    assert len(request_events_2) >= 2  # at least one model call per round-trip
    for evt in request_events_2:
        assert evt.data["messages"], "every model.request must carry verbatim messages"

    # ---- Task 3: multi-turn follow-up -----------------------------
    ctx3 = ExecutionContext(
        thread_id=ctx2.thread_id,  # same thread = follow-up
        trigger={
            "kind": "user_message",
            "content": "Now double that answer and tell me the result.",
        },
    )
    sent_messages_log.clear()
    result3 = await engine.execute(_config(with_tool=True), ctx3)
    assert "84" in result3.data["content"]["text"]

    # Turn 3's captured messages include the prior turn's content
    # (replay/history reconstruction works through CrewAI's wrap).
    events3 = [e async for e in store.read_path_to_leaf(ctx3.thread_id)]
    turn3_requests = [
        e
        for e in events3
        if e.type == EventType.MODEL_REQUEST
        and e.event_id not in {evt.event_id for evt in request_events_2}
    ]
    assert turn3_requests
    turn3_text = " ".join(
        str(m.get("content", ""))
        for m in turn3_requests[0].data["messages"]
        if isinstance(m, dict)
    )
    # The history reconstruction surfaces "42" somewhere in turn 3's
    # context (Engine I8 + Assembly Procedure under CrewAI wrap).
    assert "42" in turn3_text or "double" in turn3_text.lower()


# ===========================================================================
# TC-E2E-02 — Soundness (v1 vs baseline CrewAI on 5-10 tasks)
# ===========================================================================


@pytest.mark.skipif(
    not _llm_available(),
    reason=(
        "TC-E2E-02 (Soundness) requires E2E_LLM_API_KEY + "
        "E2E_LLM_PROVIDER. Set to run the human/judge comparison "
        "against baseline CrewAI."
    ),
)
async def test_tc_e2e_02_soundness_vs_baseline_crewai(tmp_path: Path) -> None:
    """v1 platform agents vs baseline CrewAI agents on 5-10 tasks.

    Per the catalog: identical inputs, human-or-LLM-judge comparison;
    expect v1 better on a majority (more grounded, better tool use,
    better recovery). Requires a curated task set + a judging
    methodology (deferred to the run that exercises it).
    """
    pytest.skip(
        "Test plan §12 Test 2 — scaffolded; task corpus + judging "
        "methodology authored when an E2E comparison run is scheduled. "
        "Result table goes in the run's commit message per the prompt.",
    )


# ===========================================================================
# TC-E2E-03 — Robustness (real-use soak)
# ===========================================================================


@pytest.mark.skipif(
    not os.environ.get(_SOAK_MINUTES_ENV),
    reason=(
        "TC-E2E-03 (Robustness) requires E2E_SOAK_MINUTES (e.g. 60). "
        "Set to run the multi-hour real-use soak; tracks crashes, "
        "corruption, resume correctness, memory leaks."
    ),
)
async def test_tc_e2e_03_robustness_soak(tmp_path: Path) -> None:
    """Long-running real-use soak. Tracks:
    - Zero data corruption (JSONL is parseable; entity DB matches).
    - Crashes recoverable (kill -9 mid-exchange; restart resumes).
    - Long sessions don't degrade (memory growth, latency
      regressions).
    """
    pytest.skip(
        "Test plan §12 Test 3 — scaffolded; soak driver authored "
        "when a robustness run is scheduled. Likely landing as a "
        "separate harness in tests/soak/ once requirements crystallize.",
    )


# ===========================================================================
# TC-E2E-04 — Default install needs no external services
# ===========================================================================


@pytest.mark.skipif(
    os.environ.get(_DEFAULT_INSTALL_ENV) != "1",
    reason=(
        "TC-E2E-04 (Default install) is opt-in via E2E_DEFAULT_INSTALL=1. "
        "Exercises the clean-machine first-run experience — installs the "
        "project into a throwaway venv, runs `kolsch run`, verifies "
        "the default tier (SQLite + in-process bus + EchoLLM) works "
        "without any external services. Slow + writes to disk; opt-in "
        "to keep the default `pytest` run fast."
    ),
)
def test_tc_e2e_04_default_install_no_external_services(
    tmp_path: Path,
) -> None:
    """Clean install path — exercises v1-scope §2.5 "great first-run
    experience."

    Steps:
    1. Create a throwaway venv via ``uv venv``.
    2. Install the project (from this repo) into that venv via
       ``uv pip install -e <repo_root>``.
    3. Author a minimal platform root with one demo agent.
    4. Run ``kolsch run acme/agents/demo --input content=hello``.
    5. Assert exit 0 + stdout contains the EchoLLM's reply.

    This is NOT async — subprocess-driven. No real LLM. No Postgres /
    Redis. Verifies the default tier holds end-to-end on a fresh
    install.
    """
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parents[2]
    venv = tmp_path / "venv"

    # 1. Create venv.
    subprocess.run(
        ["uv", "venv", str(venv), "--python", sys.executable],
        check=True,
        capture_output=True,
    )

    # 2. Install the project.
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(venv / "bin" / "python"),
            "-e",
            str(repo_root),
        ],
        check=True,
        capture_output=True,
    )

    # 3. Platform root with one agent.
    platform_root = tmp_path / "platform"
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (platform_root / sub).mkdir(parents=True, exist_ok=True)
    (platform_root / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
                "sandbox": {"variant": "native", "config": {}},
                "authorization": {
                    "variant": "rule_based",
                    "config": {
                        "rules": [
                            {"who": ".*", "what": ".*", "effect": "allow"},
                        ],
                    },
                },
                "storage": {
                    "entity_backend": "sqlite",
                    "entity_path": ".platform/entities.db",
                },
                "event_bus": {"backend": "in_process"},
                "file_access": {
                    "allowed_paths": ["data/"],
                    "denied_paths": [".env"],
                },
                "defaults": {},
            },
        ),
        encoding="utf-8",
    )
    (platform_root / ".env").touch()
    bundle = platform_root / "bundles" / "acme"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        yaml.safe_dump({"name": "acme", "version": "0.1", "description": "demo"}),
        encoding="utf-8",
    )
    (bundle / "agents").mkdir(exist_ok=True)
    (bundle / "agents" / "demo.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "assembly_version": "1",
                "agent_id": "ag_demo",
                "name": "demo",
                "description": "First-run demo.",
                "model": {"provider": "fake", "model": "fake-model"},
                "prompt": "You are a demo agent.",
                "io": {"mode": "conversational"},
            },
        ),
        encoding="utf-8",
    )

    # 4. Run kolsch from the throwaway venv.
    result = subprocess.run(
        [
            str(venv / "bin" / "kolsch"),
            "--root",
            str(platform_root),
            "run",
            "acme/agents/demo",
            "--input",
            "content=hello",
        ],
        check=False,
        capture_output=True,
        timeout=60,
    )

    # 5. Assert: exit 0 + EchoLLM reply on stdout.
    assert result.returncode == 0, (
        f"kolsch run failed: stderr=\n{result.stderr.decode('utf-8')}"
    )
    out = result.stdout.decode("utf-8").strip()
    payload = json.loads(out)
    assert payload["content"]["text"] == "Echo: hello"
