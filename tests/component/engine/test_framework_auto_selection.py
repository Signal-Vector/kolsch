"""spec-121 Tests 2 + 3 — AUTO framework selection + bridge lifecycle.

Boots a REAL runtime (`bootstrap_runtime`) against a tmp workspace
with a `claude_code` provider entry and a demo bundle carrying one
real `@tool`, then swaps the engine's two framework slots for spies.
No live `claude` anywhere — the subprocess spy *plays* the harness,
including (in the routing test) a REAL socket round-trip against the
engine-built `ToolBridgeServer` so the engine-side wiring
(store/bus/lock/ids/trace_id → sink → events) is proven end to end,
not stubbed.

The decision-#1 matrix (spec-121 AC2):

- `claude_code` + ≥1 tool  → `SubprocessAgentFramework` slot, bridge
  built + opened + passed via `tool_bridge=`.
- `claude_code` + 0 tools  → the default framework (spec-118 provider
  path), `tool_bridge=None`.
- any other provider       → the default framework, unaffected.

The lifecycle contract (spec-121 AC3): open ONCE before the retry
loop; a Recoverable retry reuses the same socket+token; `close()`
runs in the spawn-scoped `finally` (socket + private dir removed)
even when the framework raises Fatal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
import yaml

from engine import AgentExecutionError
from engine.context import ExecutionContext
from errors import ModelAuthenticationFailed, ModelServerError
from platform_runtime import bootstrap_runtime
from schemas import EventType
from tools.bridge.proxy import _SocketClient
from workflows.ids import TraceId

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from engine.framework import LLMHandle, Message, ToolHandle
    from platform_runtime import PlatformRuntime
    from schemas import ResolvedAgentConfig


_TOOL_SOURCE = '''
"""Demo bundle tool for spec-121's selection tests."""

from __future__ import annotations

from pydantic import BaseModel, Field

from tools.decorator import ToolContext, tool


class EchoArgs(BaseModel):
    """Args for the demo echo tool."""

    message: str = Field(description="Text to echo back.")


@tool(
    name="echo_tool",
    description="Echo a message back (spec-121 test tool).",
    args_schema=EchoArgs,
)
async def echo_tool(args: EchoArgs, ctx: ToolContext) -> dict[str, str]:
    """Return the message unchanged."""
    _ = ctx
    return {"echoed": args.message}
'''


def _agent_yaml(
    *,
    agent_id: str,
    name: str,
    provider: str,
    with_tools: bool,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": "0.1",
        "assembly_version": "1",
        "agent_id": agent_id,
        "name": name,
        "description": f"spec-121 {name} agent",
        "model": {"provider": provider, "model": "sonnet"},
        "prompt": "You are a test agent.",
        "io": {"mode": "conversational"},
    }
    if with_tools:
        doc["subsystems"] = {
            "tools": {
                "variant": "standard",
                "config": {"declared": [{"ref": "demo/tools/echo_tool"}]},
            }
        }
    return doc


def _write_workspace(root: Path) -> None:
    """Workspace: claude_code provider + demo bundle (1 tool, 3 agents)."""
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
                # A claude_code entry so pre-flight's build_llm path
                # resolves; no key (subscription-auth connector) and
                # nothing here spawns a real `claude` — the framework
                # slot is replaced by a spy before execute.
                "llm_providers": {
                    "claude_code": {"type": "claude_code"},
                },
            }
        ),
        encoding="utf-8",
    )
    (root / ".env").write_text("", encoding="utf-8")
    bundle = root / "bundles" / "demo"
    (bundle / "agents").mkdir(parents=True, exist_ok=True)
    (bundle / "tools").mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        yaml.safe_dump({"name": "demo", "version": "0.1", "description": "demo"}),
        encoding="utf-8",
    )
    (bundle / "tools" / "echo_tool.py").write_text(_TOOL_SOURCE, encoding="utf-8")
    agents = {
        "bridged": _agent_yaml(
            agent_id="ag_bridged",
            name="bridged",
            provider="claude_code",
            with_tools=True,
        ),
        "textonly": _agent_yaml(
            agent_id="ag_textonly",
            name="textonly",
            provider="claude_code",
            with_tools=False,
        ),
        "mocktools": _agent_yaml(
            agent_id="ag_mocktools",
            name="mocktools",
            provider="mock",
            with_tools=True,
        ),
    }
    for name, doc in agents.items():
        (bundle / "agents" / f"{name}.yaml").write_text(
            yaml.safe_dump(doc), encoding="utf-8"
        )


class _SpyDefaultFramework:
    """Records calls; returns a fixed reply (the spec-118-path stand-in)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run_exchange(
        self,
        *,
        system_prompt: str,
        user_message: str,
        history: Sequence[Message] = (),
        llm: LLMHandle,
        tools: Sequence[ToolHandle],
        max_iterations: int | None = None,
        cancellation: Callable[[], bool] | None = None,
        cancel_token: Any = None,
        tool_bridge: Any = None,
    ) -> str:
        self.calls.append(
            {
                "user_message": user_message,
                "tool_count": len(tools),
                "tool_bridge": tool_bridge,
            }
        )
        _ = (system_prompt, history, llm, max_iterations, cancellation, cancel_token)
        return "default-framework-reply"


class _SpySubprocessFramework:
    """Records the passed bridge; behaviour injected per test."""

    def __init__(
        self,
        behaviour: Callable[[Any], Any] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._behaviour = behaviour

    async def run_exchange(
        self,
        *,
        system_prompt: str,
        user_message: str,
        history: Sequence[Message] = (),
        llm: LLMHandle,
        tools: Sequence[ToolHandle],
        max_iterations: int | None = None,
        cancellation: Callable[[], bool] | None = None,
        cancel_token: Any = None,
        tool_bridge: Any = None,
    ) -> str:
        fragment = tool_bridge.config_fragment() if tool_bridge is not None else None
        self.calls.append(
            {
                "user_message": user_message,
                "tool_bridge": tool_bridge,
                "fragment": fragment,
                "wire_names": (
                    list(tool_bridge.wire_tool_names())
                    if tool_bridge is not None
                    else None
                ),
                "model": getattr(getattr(llm, "primary_spec", None), "model", None),
            }
        )
        _ = (
            system_prompt,
            history,
            tools,
            max_iterations,
            cancellation,
            cancel_token,
        )
        if self._behaviour is not None:
            result = self._behaviour(tool_bridge)
            if hasattr(result, "__await__"):
                return str(await result)
            return str(result)
        return "subprocess-framework-reply"


async def _boot(root: Path) -> PlatformRuntime:
    _write_workspace(root)
    return await bootstrap_runtime(root=root)


async def _run(
    runtime: PlatformRuntime,
    agent_ref: str,
    *,
    content: str = "hello",
    trace_id: TraceId | None = None,
) -> tuple[ResolvedAgentConfig, ExecutionContext, Any]:
    config = await runtime.build_resolved_config(agent_ref)
    session = await runtime.store.create_session(
        str(config.agent_id), name=f"spec121:{agent_ref}"
    )
    threads = await runtime.store.list_threads(session.session_id)
    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": content},
        trace_id=trace_id,
    )
    agent_msg = await runtime.engine.execute(config, ctx)
    return config, ctx, agent_msg


# ---------------------------------------------------------------------------
# Test 2 — the decision-#1 AUTO-selection matrix.
# ---------------------------------------------------------------------------


async def test_framework_auto_selection_claude_code_with_tools_uses_bridge(
    tmp_path: Path,
) -> None:
    """`claude_code` + tools → subprocess framework, with a LIVE bridge:
    the spy binds the real socket, lists tools, and calls the real
    demo tool through `invoke_observed`; `tool.called`/`tool.result`
    land in the store carrying the exchange's trace_id (hazard #4)."""
    runtime = await _boot(tmp_path)
    default_spy = _SpyDefaultFramework()

    async def _drive_bridge(tool_bridge: Any) -> str:
        # Play the harness: real socket, real bind, real tools/call.
        fragment = tool_bridge.config_fragment()
        assert Path(fragment["socket_path"]).exists()
        client = await _SocketClient.connect(fragment["socket_path"])
        try:
            await client.request("bind", token=fragment["token"])
            listed = await client.request("list_tools")
            names = [t["name"] for t in listed["tools"]]
            assert names == ["mcp__kolsch__echo_tool"]
            result = await client.request(
                "call_tool",
                name="mcp__kolsch__echo_tool",
                arguments={"message": "over the bridge"},
            )
        finally:
            await client.close()
        assert result["isError"] is False
        assert json.loads(result["content"][0]["text"]) == {"echoed": "over the bridge"}
        return "final text after tool use"

    subprocess_spy = _SpySubprocessFramework(behaviour=_drive_bridge)
    runtime.engine._framework = default_spy
    runtime.engine._subprocess_framework = subprocess_spy

    trace = TraceId(f"tr_{uuid4()}")
    _config, ctx, agent_msg = await _run(runtime, "demo/agents/bridged", trace_id=trace)

    # Routing: subprocess ran, default did not.
    assert len(subprocess_spy.calls) == 1
    assert default_spy.calls == []
    call = subprocess_spy.calls[0]
    assert call["tool_bridge"] is not None
    assert call["wire_names"] == ["mcp__kolsch__echo_tool"]
    # The model id crossed the seam via the LLM handle (spec-121).
    assert call["model"] == "sonnet"
    # The engine materialized the framework's text into agent.message.
    assert agent_msg.data["content"]["text"] == "final text after tool use"

    # Bridge events landed in the REAL store, stamped with the
    # exchange's trace id and the agent actor.
    events = [e async for e in runtime.store.read_path_to_leaf(ctx.thread_id)]
    types = [e.type.value for e in events]
    assert "tool.called" in types
    assert "tool.result" in types
    called = next(e for e in events if e.type == EventType.TOOL_CALLED)
    result_evt = next(e for e in events if e.type == EventType.TOOL_RESULT)
    assert called.data["tool_name"] == "echo_tool"
    assert called.data["arguments"] == {"message": "over the bridge"}
    assert result_evt.data["result"] == {"echoed": "over the bridge"}
    for evt in (called, result_evt):
        assert evt.trace_id == str(trace)
        assert evt.actor.kind == "agent"

    # Spawn-scoped close: socket + private dir removed after execute.
    fragment = call["fragment"]
    assert fragment is not None
    assert not Path(fragment["socket_path"]).exists()
    assert not Path(fragment["socket_path"]).parent.exists()


async def test_framework_auto_selection_zero_tools_stays_on_default(
    tmp_path: Path,
) -> None:
    """`claude_code` + 0 tools → the default framework (the spec-118
    text-only provider path), `tool_bridge=None`, no bridge built."""
    runtime = await _boot(tmp_path)
    default_spy = _SpyDefaultFramework()
    subprocess_spy = _SpySubprocessFramework()
    runtime.engine._framework = default_spy
    runtime.engine._subprocess_framework = subprocess_spy

    _config, _ctx, agent_msg = await _run(runtime, "demo/agents/textonly")

    assert subprocess_spy.calls == []
    assert len(default_spy.calls) == 1
    assert default_spy.calls[0]["tool_bridge"] is None
    assert default_spy.calls[0]["tool_count"] == 0
    assert agent_msg.data["content"]["text"] == "default-framework-reply"


async def test_framework_auto_selection_non_claude_code_unaffected(
    tmp_path: Path,
) -> None:
    """A non-`claude_code` agent WITH tools stays on the default
    framework — provider is the selection key, not tool count."""
    runtime = await _boot(tmp_path)
    default_spy = _SpyDefaultFramework()
    subprocess_spy = _SpySubprocessFramework()
    runtime.engine._framework = default_spy
    runtime.engine._subprocess_framework = subprocess_spy

    _config, _ctx, agent_msg = await _run(runtime, "demo/agents/mocktools")

    assert subprocess_spy.calls == []
    assert len(default_spy.calls) == 1
    assert default_spy.calls[0]["tool_bridge"] is None
    assert default_spy.calls[0]["tool_count"] == 1
    assert agent_msg.data["content"]["text"] == "default-framework-reply"


async def test_no_subprocess_framework_wired_keeps_default_path(
    tmp_path: Path,
) -> None:
    """Engine constructed WITHOUT a subprocess framework (tests,
    minimal wirings): a `claude_code`+tools agent stays on the default
    framework instead of crashing — the third AND-arm of decision #1."""
    runtime = await _boot(tmp_path)
    default_spy = _SpyDefaultFramework()
    runtime.engine._framework = default_spy
    runtime.engine._subprocess_framework = None

    _config, _ctx, agent_msg = await _run(runtime, "demo/agents/bridged")

    assert len(default_spy.calls) == 1
    assert default_spy.calls[0]["tool_bridge"] is None
    assert agent_msg.data["content"]["text"] == "default-framework-reply"


# ---------------------------------------------------------------------------
# Test 3 — bridge lifecycle: open-once, retry-reuse, close-in-finally.
# ---------------------------------------------------------------------------


async def test_bridge_lifecycle_recoverable_retry_reuses_bridge(
    tmp_path: Path,
) -> None:
    """A Recoverable failure retries against the SAME bridge — one
    `open()` before the loop, identical socket+token across attempts,
    close() once after."""
    runtime = await _boot(tmp_path)
    attempts: list[int] = []

    def _fail_once(tool_bridge: Any) -> str:
        _ = tool_bridge
        attempts.append(1)
        if len(attempts) == 1:
            raise ModelServerError(
                provider="claude_code", model="sonnet", status_code=502
            )
        return "second attempt ok"

    subprocess_spy = _SpySubprocessFramework(behaviour=_fail_once)
    runtime.engine._framework = _SpyDefaultFramework()
    runtime.engine._subprocess_framework = subprocess_spy

    _config, _ctx, agent_msg = await _run(runtime, "demo/agents/bridged")

    assert agent_msg.data["content"]["text"] == "second attempt ok"
    assert len(subprocess_spy.calls) == 2
    first, second = subprocess_spy.calls
    # Same bridge object, same socket, same token — NOT re-opened.
    assert first["tool_bridge"] is second["tool_bridge"]
    assert first["fragment"] == second["fragment"]
    # Closed exactly once, after the loop: socket + dir gone.
    assert first["fragment"] is not None
    assert not Path(first["fragment"]["socket_path"]).exists()
    assert not Path(first["fragment"]["socket_path"]).parent.exists()


async def test_bridge_lifecycle_close_runs_when_framework_raises_fatal(
    tmp_path: Path,
) -> None:
    """A Fatal raise from `run_exchange` still hits the spawn-scoped
    `finally`: the socket and its private dir are removed and the
    token is revoked even though the exchange failed."""
    runtime = await _boot(tmp_path)

    def _always_fatal(tool_bridge: Any) -> str:
        _ = tool_bridge
        raise ModelAuthenticationFailed(
            provider="claude_code", model="sonnet", status_code=401
        )

    subprocess_spy = _SpySubprocessFramework(behaviour=_always_fatal)
    runtime.engine._framework = _SpyDefaultFramework()
    runtime.engine._subprocess_framework = subprocess_spy

    config = await runtime.build_resolved_config("demo/agents/bridged")
    session = await runtime.store.create_session(str(config.agent_id), name="fatal")
    threads = await runtime.store.list_threads(session.session_id)
    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": "boom"},
    )
    with pytest.raises(AgentExecutionError):
        await runtime.engine.execute(config, ctx)

    assert len(subprocess_spy.calls) == 1
    fragment = subprocess_spy.calls[0]["fragment"]
    assert fragment is not None
    assert not Path(fragment["socket_path"]).exists()
    assert not Path(fragment["socket_path"]).parent.exists()
