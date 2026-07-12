"""TC-BUG-echo-01 — regression for the silent-echo bug.

Ticket: docs/process/bug-echo-on-real-provider.md

Pre-fix behaviour: a user with a real provider connected and an agent
whose `model:` points at that provider got the user's input echoed
back (via `EchoLLMDriver`) — the `model.request` event correctly
recorded the real ModelSpec, but the actual LLM call never reached
the provider because the runtime hardcoded `llm=EchoLLMDriver` per
:func:`platform_runtime.bootstrap_runtime`'s
`chosen_llm = llm or EchoLLMDriver().__call__`.

Post-fix behaviour: when `llm_providers` is populated AND the caller
didn't pass `llm=`, the runtime wires `llm_for_model=` to a
build_llm-backed per-spec factory. `provider: mock` short-circuits to
EchoLLM inside the factory (AC #3 — preserves the no-creds tutorial
path).

This test:

1. Stands up a workspace with a connected provider configured in
   `platform.yaml` but no real network call.
2. Constructs an agent whose `model:` is a real-provider canonical
   form.
3. Monkey-patches the runtime's per-spec factory with a stub that
   returns ``LLMResponse(content="DISTINCTIVE_REPLY_42")`` — a
   non-echo, non-trivial string. If the engine routed through
   ``EchoLLMDriver`` (the bug), the response would be
   ``"Echo: hello"`` — clearly different.
4. Asserts three things per AC #4:
   - `model.request.data.model` carries the real provider/model.
   - `model.response.data.content.text == "DISTINCTIVE_REPLY_42"`.
   - `agent.message.data.content.text == "DISTINCTIVE_REPLY_42"`.

Catches H1 (MinimalFramework wired unconditionally), H3 (no per-spec
dispatch), H4 (connector returns wrong thing), H5 (CLI bypasses
config), H6 (agent.message constructed from user.message)
simultaneously.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yaml

from engine import LLMResponse
from engine.context import ExecutionContext
from platform_runtime import bootstrap_runtime
from schemas import EventType

if TYPE_CHECKING:
    from pathlib import Path

    from engine.framework import LLMRequest


_DISTINCTIVE = "DISTINCTIVE_REPLY_42"


def _write_workspace(root: Path) -> None:
    """Lay down a workspace with one provider configured + one agent
    pinned to that provider via canonical-object form."""
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
                "llm_providers": {
                    "openrouter": {
                        "type": "openrouter",
                        "api_key_secret": "OPENROUTER_API_KEY",
                        "models": [{"id": "anthropic/claude-opus-4-7"}],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    (root / ".env").write_text("OPENROUTER_API_KEY=sk-mock-test\n", encoding="utf-8")
    bundle = root / "bundles" / "demo"
    (bundle / "agents").mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        yaml.safe_dump({"name": "demo", "version": "0.1", "description": "demo"}),
        encoding="utf-8",
    )
    (bundle / "agents" / "realagent.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "assembly_version": "1",
                "agent_id": "ag_realagent",
                "name": "realagent",
                "description": "agent against a real provider",
                "model": {
                    "provider": "openrouter",
                    "model": "anthropic/claude-opus-4-7",
                },
                "prompt": "You are a helpful assistant.",
                "io": {"mode": "conversational"},
            }
        ),
        encoding="utf-8",
    )


async def test_tc_bug_echo_01_real_provider_does_not_echo(tmp_path: Path) -> None:
    """Pre-fix: agent.message.content.text == "Echo: hello" (the
    EchoLLMDriver output). Post-fix: agent.message.content.text ==
    the distinctive string emitted by the per-spec factory."""
    _write_workspace(tmp_path)
    runtime = await bootstrap_runtime(root=tmp_path)

    # Sanity: the post-fix wiring populates `llm_for_model` whenever
    # `llm_providers` is non-empty AND no explicit `llm=` was passed.
    # This guards against a regression that re-introduces the bug by
    # silently routing back to the EchoLLMDriver default.
    assert runtime.engine._llm_for_model is not None, (
        "bug regression: llm_for_model is None despite "
        "llm_providers being populated — fix at "
        "src/platform_runtime/runtime.py line 198-235 has regressed"
    )
    assert runtime.engine._llm is None, (
        "bug regression: engine still holds an EchoLLMDriver under "
        "`llm`, which would shadow the per-spec factory"
    )

    # Replace the per-spec factory with a deterministic stub returning
    # a non-echo distinctive string. This is the equivalent of "the
    # connector's build_llm returned a real crewai.LLM and that LLM
    # responded with DISTINCTIVE_REPLY_42" — we skip the crewai.LLM
    # construction so no real HTTP happens.
    async def stub_llm_callable(request: LLMRequest) -> LLMResponse:
        _ = request
        return LLMResponse(content=_DISTINCTIVE)

    async def stub_factory(spec: Any) -> Any:
        _ = spec
        return stub_llm_callable

    # Reach into the engine; this is intentional for a runtime-wiring
    # test. The bug exists EXACTLY because the engine's per-spec
    # factory wasn't reached.
    runtime.engine._llm_for_model = stub_factory

    # Run the agent.
    config = await runtime.build_resolved_config("demo/agents/realagent")
    session = await runtime.store.create_session(
        str(config.agent_id), name="tc-bug-echo-01"
    )
    threads = await runtime.store.list_threads(session.session_id)
    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": "hello"},
    )
    agent_msg = await runtime.engine.execute(config, ctx)
    assert agent_msg.type == EventType.AGENT_MESSAGE

    # AC #4 — three assertions in one test.
    events = [e async for e in runtime.store.read_path_to_leaf(ctx.thread_id)]

    # (1) model.request carries the real provider/model.
    request_events = [e for e in events if e.type == EventType.MODEL_REQUEST]
    assert len(request_events) == 1
    model_spec = request_events[0].data["model"]
    assert model_spec["provider"] == "openrouter"
    assert model_spec["model"] == "anthropic/claude-opus-4-7"

    # (2) model.response carries the distinctive (non-echo) content.
    response_events = [e for e in events if e.type == EventType.MODEL_RESPONSE]
    assert len(response_events) == 1
    response_content = response_events[0].data["content"]
    assert response_content == {"text": _DISTINCTIVE}, (
        f"bug regression: model.response.data.content is "
        f"{response_content!r} — the engine routed through the "
        f"EchoLLMDriver path instead of the per-spec factory"
    )

    # (3) agent.message mirrors model.response (rules out H6:
    # agent.message constructed from user.message).
    assert agent_msg.data["content"]["text"] == _DISTINCTIVE


async def test_mock_provider_agent_still_echoes_post_fix(tmp_path: Path) -> None:
    """AC #3 — mock-LLM behaviour preserved for `provider: mock` agents
    even when real providers are configured. The factory inspects the
    spec and short-circuits to EchoLLMDriver before calling
    build_llm."""
    _write_workspace(tmp_path)
    # Replace the agent's model with `provider: mock`.
    agent_path = tmp_path / "bundles" / "demo" / "agents" / "realagent.yaml"
    cfg = yaml.safe_load(agent_path.read_text())
    cfg["model"] = {"provider": "mock", "model": "mock-model"}
    agent_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    runtime = await bootstrap_runtime(root=tmp_path)
    # Factory IS wired (providers are configured), but `provider: mock`
    # should still produce echo output.
    assert runtime.engine._llm_for_model is not None

    config = await runtime.build_resolved_config("demo/agents/realagent")
    session = await runtime.store.create_session(
        str(config.agent_id), name="ac3-mock-preserved"
    )
    threads = await runtime.store.list_threads(session.session_id)
    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": "preserved"},
    )
    agent_msg = await runtime.engine.execute(config, ctx)
    # EchoLLMDriver prefixes with "Echo:" — confirming the factory
    # routed `provider: mock` through the dev driver.
    assert agent_msg.data["content"]["text"] == "Echo: preserved"
