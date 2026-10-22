"""Component tests — kolsch tools called through the MCP bridge obey kolsch authz.

bug-158 / owner decision F4: a `claude_code` agent with tools runs its
own agentic loop and calls kolsch tools over the spec-120 bridge. Those
calls go through kolsch authorization with the agent's exchange chain,
exactly like in-process calls: a platform deny blocks them, and a
`from:`-scoped rule matches the calling agent only. (Claude Code's own
native tools never reach kolsch and are outside its governance.)

Boots a real runtime against a tmp workspace with a `claude_code`
provider and a demo bundle tool, replaces the engine's subprocess
framework with a spy that plays the harness over the REAL engine-built
`ToolBridgeServer` socket, and asserts on the stored `tool.*` events.
No live `claude`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yaml

from engine.context import ExecutionContext
from platform_runtime import bootstrap_runtime
from schemas import EventType
from tools.bridge.proxy import _SocketClient

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from platform_runtime import PlatformRuntime
    from schemas import Event

_AGENT_A = "demo/agents/bridged_a"
_AGENT_B = "demo/agents/bridged_b"
_WIRE_NAME = "mcp__kolsch__echo_tool"

_TOOL_SOURCE = '''
"""Demo bundle tool for bug-158's bridge tests."""

from __future__ import annotations

from pydantic import BaseModel, Field

from tools.decorator import ToolContext, tool


class EchoArgs(BaseModel):
    """Args for the demo echo tool."""

    message: str = Field(description="Text to echo back.")


@tool(
    name="echo_tool",
    description="Echo a message back (bug-158 test tool).",
    args_schema=EchoArgs,
)
async def echo_tool(args: EchoArgs, ctx: ToolContext) -> dict[str, str]:
    """Return the message unchanged."""
    _ = ctx
    return {"echoed": args.message}
'''


def _write_workspace(root: Path, *, rules: list[dict[str, Any]]) -> None:
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (root / sub).mkdir(exist_ok=True)
    (root / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
                "sandbox": {"variant": "native", "config": {}},
                "authorization": {
                    "variant": "rule_based",
                    "config": {"rules": rules},
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
                "llm_providers": {"claude_code": {"type": "claude_code"}},
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
    for name in ("bridged_a", "bridged_b"):
        (bundle / "agents" / f"{name}.yaml").write_text(
            yaml.safe_dump(
                {
                    "schema_version": "0.1",
                    "assembly_version": "1",
                    "agent_id": f"ag_{name}",
                    "name": name,
                    "description": f"bug-158 {name} agent",
                    "model": {"provider": "claude_code", "model": "sonnet"},
                    "prompt": "You are a test agent.",
                    "io": {"mode": "conversational"},
                    "subsystems": {
                        "tools": {
                            "variant": "standard",
                            "config": {"declared": [{"ref": "demo/tools/echo_tool"}]},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )


class _HarnessSpy:
    """Plays `claude -p`: binds the engine's bridge and calls `echo_tool`.

    The call frame also carries chain-like fields naming the OTHER
    agent — the bridge must ignore them.
    """

    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []

    async def run_exchange(self, *, tool_bridge: Any = None, **_: Any) -> str:
        fragment = tool_bridge.config_fragment()
        client = await _SocketClient.connect(fragment["socket_path"])
        try:
            await client.request("bind", token=fragment["token"])
            result = await client.request(
                "call_tool",
                name=_WIRE_NAME,
                arguments={"message": "over the bridge"},
                caller_chain=[_AGENT_B],
            )
        finally:
            await client.close()
        self.results.append(result)
        return "done"


async def _run_bridged(runtime: PlatformRuntime, agent_ref: str) -> list[Event]:
    """One exchange of `agent_ref` whose harness makes one bridged call."""
    runtime.engine._subprocess_framework = _HarnessSpy()
    config = await runtime.build_resolved_config(agent_ref)
    session = await runtime.store.create_session(str(config.agent_id), name=agent_ref)
    threads = await runtime.store.list_threads(session.session_id)
    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": "echo something"},
    )
    await runtime.engine.execute(config, ctx)
    return [e async for e in runtime.store.read_path_to_leaf(ctx.thread_id)]


def _tool_terminal(events: Sequence[Event]) -> Event:
    (terminal,) = [
        e for e in events if e.type in (EventType.TOOL_RESULT, EventType.TOOL_FAILED)
    ]
    return terminal


async def test_platform_deny_blocks_bridged_kolsch_tool(tmp_path: Path) -> None:
    """A platform deny on the kolsch tool blocks the bridged call; the
    denial records the calling agent's chain."""
    _write_workspace(
        tmp_path,
        rules=[
            {"what": "agent.execute.*", "pattern": "*", "effect": "allow"},
            {"what": "demo/tools/echo_tool", "pattern": "*", "effect": "deny"},
            {"what": "*", "pattern": "*", "effect": "allow"},
        ],
    )
    runtime = await bootstrap_runtime(root=tmp_path)

    terminal = _tool_terminal(await _run_bridged(runtime, _AGENT_A))

    assert terminal.type == EventType.TOOL_FAILED
    error = terminal.data["error"]
    assert error["type"] == "tool.unauthorized"
    assert error["detail"]["caller_chain"] == [_AGENT_A]


async def test_agent_scoped_deny_blocks_only_that_bridged_agent(
    tmp_path: Path,
) -> None:
    """An A-scoped deny above a broad allow blocks A's bridged call and
    leaves B's alone — the bridge authorizes with the exchange chain,
    not a chain named on the wire (A's frame names B)."""
    _write_workspace(
        tmp_path,
        rules=[
            {
                "what": "demo/tools/echo_tool",
                "pattern": "*",
                "from": _AGENT_A,
                "effect": "deny",
            },
            {"what": "*", "pattern": "*", "effect": "allow"},
        ],
    )
    runtime = await bootstrap_runtime(root=tmp_path)

    a_terminal = _tool_terminal(await _run_bridged(runtime, _AGENT_A))
    b_terminal = _tool_terminal(await _run_bridged(runtime, _AGENT_B))

    assert a_terminal.type == EventType.TOOL_FAILED
    a_error = a_terminal.data["error"]
    assert a_error["type"] == "tool.unauthorized"
    assert a_error["detail"]["matched_rule"]["from_"] == _AGENT_A
    assert a_error["detail"]["caller_chain"] == [_AGENT_A]

    assert b_terminal.type == EventType.TOOL_RESULT
    assert b_terminal.data["result"] == {"echoed": "over the bridge"}
