"""Bug-engine-tools-wiring-gap regression — TC-TOOL-WIRE-01.

Pins the load-bearing wiring this bug fix restores: an agent that
declares tools in ``subsystems.tools.config.declared`` must have
those tools reach the framework, get exercised by the model's
``tool_use`` calls, and produce REAL output in the final
``agent.message`` — NOT the hallucinated Linux-root listing the
user reported.

The test drives the bootstrap_runtime path (the same path
``kolsch run`` and ``kolsch chat`` hit in production) so the
regression catches a re-introduction of the wiring gap at the
same call site the user's bug exercised. A simulated FakeLLM
emits the ``tool_use`` call; the rest of the chain (tool dispatch,
sandbox, real ``bash`` execution, the model's second turn with the
tool result, the final ``agent.message``) is the production code
path.

Per the bug doc's coordinator resolution to Q1 (ship option (b)):
the engine's pre-flight raises ``config.unknown_tool`` when a
declared ref doesn't resolve. Two tests cover this: the happy
path (TC-TOOL-WIRE-01) and the unknown-ref path
(TC-TOOL-WIRE-02).
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
import yaml

from engine import (
    ExecutionContext,
    LLMRequest,
    LLMResponse,
)
from errors import ConfigUnknownTool
from platform_runtime import bootstrap_runtime
from schemas import EventType
from tests.fixtures.fake_llm import (
    FakeLLM,
    TextResponse,
    ToolCallResponse,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


# ---------------------------------------------------------------------------
# Workspace fixture — initialised once per session via `kolsch setup`
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def initialized_workspace_for_tool_wiring(
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """A workspace scaffolded via ``kolsch setup --non-interactive``.

    Uses subprocess invocation to mirror the production path that
    the user's repro hit; in-process invocation would skip the
    bundle-copy step that lands the four declared tool files.
    """
    root = tmp_path_factory.mktemp("tool_wiring_workspace")
    env = dict(os.environ)
    env["KOLSCH_WORKSPACE"] = str(root)
    env["PATH"] = "/usr/bin:/bin"  # bash needs to find executables
    result = subprocess.run(
        [sys.executable, "-m", "cli", "setup", "--non-interactive"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        msg = (
            f"setup failed: rc={result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        raise RuntimeError(msg)
    return root


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _allow_bash_in_workspace(root: Path) -> None:
    """Rewrite platform.yaml + helper.yaml to allow ``tool.bash`` outright.

    The default rule set has ``tool.bash`` as ``prompt`` (the
    workspace assumes the Phase 2.1 chat modal will resolve it).
    For an end-to-end regression test driven by FakeLLM there's
    no modal — the default ``prompt`` collapses to ``deny`` (no
    callback), the bash call fails, and we can't observe the
    real-stdout property the bug fixes.

    Rewriting to ``allow`` here is the test-fixture-equivalent of
    a user running ``kolsch permissions add --who '.*' --what
    'tool.bash' --effect allow``. The test doesn't pin the
    modal-resolution path; that's Phase 2.1's TC-CHAT-03/04.

    Post-Phase-2.2 (and per qol-tool-permission-discovery-uniform-
    with-stacking.md Step 2) the resolver composes the four-level
    stack agent → bundle → platform → default. The starter
    ``helper.yaml`` ships an agent-level bash → prompt rule; that
    rule wins over the platform layer in a no-callback (non-chat)
    context and collapses to deny. So the fixture flips BOTH
    layers' prompt → allow.

    qol-action-namespace-bundle-scoped.md Step 2 — the bash rule
    is identified by either the legacy regex shape
    (``what: tool\\.bash``, pre-migration) OR the 3-seg form
    (``what: kolsch/tools/bash``, post-migration); both shapes are
    accepted so the fixture works regardless of which startup
    migrations have run.
    """
    bash_rule_whats = (r"tool\.bash", "kolsch/tools/bash")

    yaml_path = root / "platform.yaml"
    payload = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    rules = payload["authorization"]["config"]["rules"]
    for rule in rules:
        if rule.get("what") in bash_rule_whats and rule.get("effect") == "prompt":
            rule["effect"] = "allow"
            break
    yaml_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    # Agent-layer rule flip — same target.
    helper_path = root / "bundles" / "kolsch" / "agents" / "helper.yaml"
    if helper_path.is_file():
        agent_payload = yaml.safe_load(helper_path.read_text(encoding="utf-8")) or {}
        agent_rules = agent_payload.get("permissions") or []
        for rule in agent_rules:
            if rule.get("what") in bash_rule_whats and rule.get("effect") == "prompt":
                rule["effect"] = "allow"
                break
        helper_path.write_text(
            yaml.safe_dump(agent_payload, sort_keys=False), encoding="utf-8"
        )


def _make_fake_llm_callable(
    fake: FakeLLM,
) -> Callable[[LLMRequest], Awaitable[LLMResponse]]:
    """Adapt :class:`FakeLLM` to the engine's ``llm=`` callable shape.

    Mirrors the adapter in ``tests/component/test_engine.py`` so
    tool-call responses go through the framework's standard
    ``tool_use`` path.
    """

    async def _llm(request: LLMRequest) -> LLMResponse:
        context: dict[str, object] = {
            "messages": [m.model_dump() for m in request.messages],
            "tools": list(request.tools),
            "model": request.model,
            "system_prompt": next(
                (m.content for m in request.messages if m.role == "system"),
                "",
            ),
        }
        response = await fake.agenerate(context)
        if isinstance(response, TextResponse):
            return LLMResponse(content=response.text)
        if isinstance(response, ToolCallResponse):
            return LLMResponse(
                content=None,
                tool_calls=[
                    {
                        "id": response.tool_call_id or f"tc_{uuid4()}",
                        "name": response.tool,
                        "arguments": dict(response.args),
                    },
                ],
            )
        msg = f"unexpected FakeLLM response shape: {response!r}"
        raise RuntimeError(msg)

    return _llm


# ===========================================================================
# TC-TOOL-WIRE-01 — declared tools reach the framework and run for real
# ===========================================================================


async def test_tc_tool_wire_01_helper_agent_routes_bash_through_engine_to_real_stdout(
    initialized_workspace_for_tool_wiring: Path,
) -> None:
    """TC-TOOL-WIRE-01 — bug-engine-tools-wiring-gap.md acceptance
    criteria 1 + 2 + 6 in one test.

    Setup: a workspace scaffolded with ``kolsch setup``, which
    landed the helper agent declaring all four builtin tool refs
    in ``subsystems.tools.config.declared``. The authz config is
    tweaked so ``tool.bash`` is ``allow`` rather than ``prompt``
    (the production modal path is exercised by TC-CHAT-03/04).

    Action: FakeLLM scripted to invoke
    ``bash(command="echo hello-from-tool-wire-01")`` then return a
    final text response.

    Expected:
      (a) ``tool.called`` event present with ``tool_name="bash"``.
      (b) ``tool.result`` event present carrying the real stdout
          (the literal string ``hello-from-tool-wire-01``).
      (c) The final ``agent.message`` content reflects the
          model's response that included the tool result.
      (d) The hallucination signature ``bin boot dev etc`` is
          NOT in the final agent message.

    Pre-fix verification: stashing the engine.py change makes (a)
    and (b) absent (no tool registered → model sees no tools →
    response has no tool_calls; engine treats the script's
    ``ToolCallResponse`` as text content and the test fails at
    the (a) assertion). The on-disk JSONL would match the user's
    repro exactly.
    """
    root = initialized_workspace_for_tool_wiring
    _allow_bash_in_workspace(root)

    fake = FakeLLM(
        script=[
            ToolCallResponse(
                tool="bash",
                args={"command": "echo hello-from-tool-wire-01"},
                tool_call_id="tc_wire01",
            ),
            TextResponse(text="Done. The command printed: hello-from-tool-wire-01"),
        ],
    )

    runtime = await bootstrap_runtime(root=root, llm=_make_fake_llm_callable(fake))
    config = await runtime.build_resolved_config("kolsch/agents/helper")
    session = await runtime.store.create_session(
        str(config.agent_id),
        name="tc-tool-wire-01",
    )
    threads = await runtime.store.list_threads(session.session_id)

    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={
            "kind": "user_message",
            "content": "run echo hello-from-tool-wire-01",
        },
    )
    events = [evt async for evt in runtime.engine.execute_stream(config, ctx)]

    types = [e.type for e in events]
    # (a) tool.called appeared.
    assert EventType.TOOL_CALLED in types, (
        f"expected tool.called event; got only {types}. "
        f"This is the canonical signature of bug-engine-tools-wiring-gap: "
        f"declared tools never reached the framework, so the model "
        f"emitted no tool_use call."
    )
    # (b) tool.result with real stdout.
    tool_results = [e for e in events if e.type == EventType.TOOL_RESULT]
    assert len(tool_results) >= 1, "expected tool.result for bash invocation"
    bash_result_payload = tool_results[-1].data.get("result", {})
    # The bash tool returns ``{stdout, stderr, exit_code, ...}``;
    # the literal echo string must appear in stdout.
    assert "hello-from-tool-wire-01" in str(bash_result_payload), (
        f"expected real bash stdout in tool.result; got {bash_result_payload!r}"
    )
    # (c) Final agent.message includes the model's reply.
    agent_msgs = [e for e in events if e.type == EventType.AGENT_MESSAGE]
    assert len(agent_msgs) >= 1, "expected at least one agent.message"
    final_text = str(agent_msgs[-1].data.get("content", {}).get("text", ""))
    assert "hello-from-tool-wire-01" in final_text, (
        f"final agent.message should reflect the tool result; got {final_text!r}"
    )
    # (d) NOT the hallucination signature the user reported.
    assert "bin boot dev etc" not in final_text, (
        f"final agent.message contains the hallucination signature — "
        f"bug-engine-tools-wiring-gap.md has regressed. Output: {final_text!r}"
    )


# ===========================================================================
# TC-TOOL-WIRE-02 — declared ref that doesn't resolve raises config.unknown_tool
# ===========================================================================


async def test_tc_tool_wire_02_unknown_declared_ref_raises_config_unknown_tool(
    initialized_workspace_for_tool_wiring: Path,
) -> None:
    """TC-TOOL-WIRE-02 — bug-engine-tools-wiring-gap.md acceptance
    criterion 4.

    Per coordinator resolution to Q1, this fires at engine
    pre-flight (option (b)). FUL-1 tracks the validator-side
    enforcement (option (a) — agent-load time).

    Setup: build a resolved config that declares a ref the
    catalog doesn't know.

    Expected: ``execute_stream`` yields exactly one
    ``thread.exchange_failed`` event whose error code is
    ``config.unknown_tool``.
    """
    from schemas import (
        AgentId,
        IOSpec,
        ModelSpec,
        ResolvedAgentConfig,
        SubsystemConfig,
    )

    root = initialized_workspace_for_tool_wiring
    runtime = await bootstrap_runtime(root=root)

    bad_config = ResolvedAgentConfig(
        schema_version="0.1",
        assembly_version="1",
        agent_id=AgentId("ag_bad_tools"),
        name="bad-tools",
        description="declares a tool that doesn't exist",
        model=ModelSpec(provider="fake", model="fake-model"),
        prompt="...",
        io=IOSpec(mode="conversational"),
        subsystems={
            "tools": SubsystemConfig(
                variant="standard",
                config={
                    "declared": [
                        {"ref": "kolsch/does_not_exist"},
                    ],
                },
            ),
        },
    )
    # Need an authz rule for the synthetic agent id.
    yaml_path = root / "platform.yaml"
    payload = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    payload["authorization"]["config"]["rules"].insert(
        0,
        {
            "who": ".*",
            "what": "agent\\.execute\\..*",
            "effect": "allow",
        },
    )
    yaml_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    # Re-bootstrap so the new authz rule is loaded.
    runtime = await bootstrap_runtime(root=root)

    session = await runtime.store.create_session(
        str(bad_config.agent_id),
        name="tc-tool-wire-02",
    )
    threads = await runtime.store.list_threads(session.session_id)
    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": "hi"},
    )

    events = [evt async for evt in runtime.engine.execute_stream(bad_config, ctx)]
    failed = [e for e in events if e.type == EventType.THREAD_EXCHANGE_FAILED]
    assert len(failed) == 1, (
        f"expected exactly one thread.exchange_failed event; got {len(failed)} of "
        f"types {[e.type for e in events]}"
    )
    error = failed[0].data["error"]
    assert error["type"] == "config.unknown_tool", (
        f"expected config.unknown_tool; got {error['type']!r}. Bug-engine-tools-"
        f"wiring-gap.md Q1 resolution: engine-side enforcement. If this fails, "
        f"_register_tools_with_subsystem's AssetsNotFound→ConfigUnknownTool wrap "
        f"has regressed."
    )
    # The detail names the bad ref + available refs for actionability.
    assert "does_not_exist" in error["message"]
    assert "available" in error["message"].lower()


# ===========================================================================
# Direct typed-error test — exercises the ConfigUnknownTool class itself
# ===========================================================================


async def test_engine_preflight_registers_declared_tools_with_subsystem(
    initialized_workspace_for_tool_wiring: Path,
) -> None:
    """Bug-engine-tools-wiring-gap.md Step E follow-up.

    Pins the post-pre-flight registration state directly. The
    bug doc's Step E imagined a ``bound_tools`` field on
    ExecutionContext; the actual architecture puts the
    registration on ``ToolsSubsystem._registry`` (the single
    source of truth for what's registered per agent — Engine
    I8 keeps it on the long-lived subsystem, not on the
    per-exchange context).

    This test runs an exchange with no tool_use response, then
    asserts ``list_registered_tools(agent_id)`` shows every
    tool the agent's config declared. TC-TOOL-WIRE-01 already
    proves registration works at the event-emission level
    (tool.called fires); this test proves it at the
    subsystem-state level so a future refactor that breaks
    the registration without breaking emission would still
    fail loudly.
    """
    from schemas import AgentId

    root = initialized_workspace_for_tool_wiring
    # Text-only FakeLLM response — no tool_use, so the engine
    # runs pre-flight + a single model turn + finalize. The
    # pre-flight registration is what we're pinning.
    fake = FakeLLM(script=[TextResponse(text="acknowledged.")])

    runtime = await bootstrap_runtime(root=root, llm=_make_fake_llm_callable(fake))
    config = await runtime.build_resolved_config("kolsch/agents/helper")
    session = await runtime.store.create_session(
        str(config.agent_id),
        name="preflight-registration-test",
    )
    threads = await runtime.store.list_threads(session.session_id)

    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": "hi"},
    )
    # Drain the exchange so pre-flight runs.
    async for _ in runtime.engine.execute_stream(config, ctx):
        pass

    registered = runtime.tools_subsystem.list_registered_tools(
        AgentId(str(config.agent_id))
    )
    names = {t.name for t in registered}
    # The helper agent declares all four refs in its yaml.
    expected = {"read_file", "write_file", "list_files", "bash"}
    assert expected.issubset(names), (
        f"engine pre-flight should have registered declared tools "
        f"{expected}; subsystem shows {names}. Bug-engine-tools-"
        f"wiring-gap.md Step E has regressed."
    )


def test_config_unknown_tool_carries_actionable_detail() -> None:
    """The :class:`ConfigUnknownTool` error envelope carries the bad
    ref + the available refs in its detail. Pins the error shape
    so future error-catalog edits don't strip the actionable
    fields."""
    err = ConfigUnknownTool(
        ref="kolsch/tools/ghost",
        available_refs=["kolsch/tools/read_file", "kolsch/tools/bash"],
        agent_id="ag_test",
    )
    payload: dict[str, Any] = err.to_dict()
    assert payload["type"] == "config.unknown_tool"
    assert "ghost" in payload["message"]
    assert "kolsch/tools/read_file" in payload["message"]
    assert payload["detail"]["ref"] == "kolsch/tools/ghost"
    assert payload["detail"]["agent_id"] == "ag_test"
