"""spec-121 Tests 4 + 5 — `SubprocessAgentFramework` choreography + cancel.

Drives the REAL framework against a REAL `ToolBridgeServer` (stub
tools subsystem, no-op emit) with an injected async-spawn stub — no
live `claude`. Asserts the full argv/env/cwd contract (Test 4):
inline `--mcp-config` with socket+token in the proxy entry's env,
`--allowedTools` = the DENYLIST-FILTERED wire names, `--model` from
the LLM handle's `primary_spec`, neutral cwd, scrubbed env, no
`--bare`, no `--disallowedTools`. And the cancel contract (Test 5):
firing the token mid-run kills the child, reaps it, and surfaces
`asyncio.CancelledError` (the engine's standard cancel signal).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from engine.context import CancelToken
from engine.subprocess_framework import (
    ClaudeCodeHarness,
    SubprocessAgentFramework,
)
from errors import (
    ModelAuthenticationFailed,
    ModelBinaryUnavailable,
    ModelServerError,
    PlatformInternalError,
)
from tools.bridge import ToolBridgeServer
from tools.decorator import ToolInfo

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from engine.framework import LLMRequest, LLMResponse, Message

_SCHEMA = {"type": "object", "properties": {}, "title": "NoArgs"}


class _StubSubsystem:
    """Catalog with one plain tool + both denylisted workflow tools."""

    def list_registered_tools(self, agent_id: str) -> list[ToolInfo]:
        _ = agent_id
        return [
            ToolInfo(name="echo", description="Echo.", args_schema=_SCHEMA),
            ToolInfo(name="run_workflow", description="Run.", args_schema=_SCHEMA),
            ToolInfo(
                name="cancel_workflow", description="Cancel.", args_schema=_SCHEMA
            ),
        ]


async def _noop_emit(**_kwargs: Any) -> None:
    return None


@pytest.fixture
async def bridge() -> AsyncIterator[ToolBridgeServer]:
    server = ToolBridgeServer(
        agent_id="ag_test",
        session_id="ses_test",
        thread_id="th_test",
        tools_subsystem=_StubSubsystem(),
        emit=_noop_emit,
    )
    await server.open()
    try:
        yield server
    finally:
        await server.close()


class _HandleWithSpec:
    """LLMHandle-shaped stub carrying a `primary_spec` (the model channel)."""

    class _Spec:
        model = "opus"

    primary_spec = _Spec()

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        _ = request
        msg = "the subprocess framework must not invoke the LLM seam"
        raise AssertionError(msg)


class _BareHandle:
    """LLMHandle stub WITHOUT `primary_spec` — falls back to the default."""

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        _ = request
        msg = "the subprocess framework must not invoke the LLM seam"
        raise AssertionError(msg)


class _StubProc:
    """Canned `_HarnessProcess`: records `kill()`, returns fixed output."""

    def __init__(
        self,
        *,
        stdout: bytes,
        returncode: int = 0,
        block_until_killed: bool = False,
    ) -> None:
        self._stdout = stdout
        self._returncode: int | None = None
        self._final_returncode = returncode
        self._block = block_until_killed
        self._killed = asyncio.Event()
        self.kill_calls = 0
        self.reaped = False

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def kill(self) -> None:
        self.kill_calls += 1
        self._killed.set()

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._block:
            await self._killed.wait()
            self._returncode = -9
        else:
            self._returncode = self._final_returncode
        self.reaped = True
        return self._stdout, b""


class _CaptureSpawner:
    """Records argv/cwd/env; hands back the prepared `_StubProc`."""

    def __init__(self, proc: _StubProc) -> None:
        self.proc = proc
        self.argv: list[str] | None = None
        self.cwd: str | None = None
        self.env: dict[str, str] | None = None

    async def __call__(
        self,
        argv: list[str],
        *,
        cwd: str,
        env: Mapping[str, str],
    ) -> _StubProc:
        self.argv = list(argv)
        self.cwd = cwd
        self.env = dict(env)
        return self.proc


def _framework(spawner: _CaptureSpawner) -> SubprocessAgentFramework:
    return SubprocessAgentFramework(descriptor=ClaudeCodeHarness(), spawner=spawner)


_SUCCESS_STDOUT = json.dumps(
    {"is_error": False, "result": "bridged reply from claude"}
).encode()


# ---------------------------------------------------------------------------
# Test 4 — choreography: argv / env / cwd / result extraction.
# ---------------------------------------------------------------------------


async def test_subprocess_framework_choreography_argv_env_cwd(
    bridge: ToolBridgeServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-scrubbed")
    spawner = _CaptureSpawner(_StubProc(stdout=_SUCCESS_STDOUT))
    out = await _framework(spawner).run_exchange(
        system_prompt="be terse",
        user_message="use the echo tool",
        llm=_HandleWithSpec(),
        tools=[],
        cancel_token=CancelToken(),
        tool_bridge=bridge,
    )
    assert out == "bridged reply from claude"

    argv = spawner.argv
    assert argv is not None
    # Shared prefix (build_base_argv) — spec-118 posture preserved.
    assert argv[0] == "claude"
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "opus"  # from primary_spec
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert "--bare" not in argv
    # Framework tool-mode flags — NOT the provider's text-only flag.
    assert "--disallowedTools" not in argv
    fragment = bridge.config_fragment()
    mcp_config = json.loads(argv[argv.index("--mcp-config") + 1])
    entry = mcp_config["mcpServers"]["kolsch"]
    assert entry["command"] == sys.executable
    assert entry["args"] == ["-m", "cli", "mcp", "bridge"]
    assert entry["env"] == {
        "KOLSCH_MCP_SOCKET": fragment["socket_path"],
        "KOLSCH_MCP_TOKEN": fragment["token"],
    }
    # Denylist-filtered wire names only (decision #2): the workflow
    # tools never reach --allowedTools.
    assert argv[argv.index("--allowedTools") + 1] == "mcp__kolsch__echo"
    # System prompt = agent prompt + the harness tool-grounding
    # preamble (live-run finding: the harness may DEFER MCP tools
    # behind tool search; the preamble names them so the model loads
    # + calls them instead of substituting builtins).
    appended = argv[argv.index("--append-system-prompt") + 1]
    assert appended.startswith("be terse")
    assert "mcp__kolsch__echo" in appended
    # Positional body last.
    assert argv[-1] == "use the echo tool"

    # Neutral cwd: a real (temp) dir, not the invoking workspace.
    assert spawner.cwd is not None
    assert spawner.cwd != str(Path.cwd())
    assert "kolsch-claude-" in spawner.cwd
    # Scrubbed env: the redirect var never reaches the child.
    assert spawner.env is not None
    assert "ANTHROPIC_API_KEY" not in spawner.env
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-should-be-scrubbed"


async def test_subprocess_framework_history_renders_transcript(
    bridge: ToolBridgeServer,
) -> None:
    from engine.framework import Message as _Message

    history: list[Message] = [
        _Message(role="user", content="first question"),
        _Message(role="assistant", content="first answer"),
    ]
    spawner = _CaptureSpawner(_StubProc(stdout=_SUCCESS_STDOUT))
    await _framework(spawner).run_exchange(
        system_prompt="",
        user_message="follow-up",
        history=history,
        llm=_BareHandle(),
        tools=[],
        tool_bridge=bridge,
    )
    argv = spawner.argv
    assert argv is not None
    body = argv[-1]
    assert body == "User: first question\n\nAssistant: first answer\n\nUser: follow-up"
    # Empty agent system prompt → the appended prompt is the harness
    # tool-grounding preamble alone (tools are registered).
    appended = argv[argv.index("--append-system-prompt") + 1]
    assert appended.startswith("Your platform tools for this run")
    # No primary_spec on the handle → descriptor default model.
    assert argv[argv.index("--model") + 1] == "sonnet"


async def test_subprocess_framework_tool_bridge_none_is_typed_error() -> None:
    spawner = _CaptureSpawner(_StubProc(stdout=_SUCCESS_STDOUT))
    with pytest.raises(PlatformInternalError):
        await _framework(spawner).run_exchange(
            system_prompt="",
            user_message="hi",
            llm=_BareHandle(),
            tools=[],
            tool_bridge=None,
        )
    assert spawner.argv is None  # nothing was spawned


async def test_subprocess_framework_missing_binary_maps_unavailable(
    bridge: ToolBridgeServer,
) -> None:
    class _MissingSpawner(_CaptureSpawner):
        async def __call__(
            self,
            argv: list[str],
            *,
            cwd: str,
            env: Mapping[str, str],
        ) -> _StubProc:
            _ = (argv, cwd, env)
            raise FileNotFoundError

    with pytest.raises(ModelBinaryUnavailable):
        await _framework(_MissingSpawner(_StubProc(stdout=b""))).run_exchange(
            system_prompt="",
            user_message="hi",
            llm=_BareHandle(),
            tools=[],
            tool_bridge=bridge,
        )


async def test_subprocess_framework_nonzero_exit_maps_server_error(
    bridge: ToolBridgeServer,
) -> None:
    spawner = _CaptureSpawner(_StubProc(stdout=b"boom", returncode=3))
    with pytest.raises(ModelServerError) as excinfo:
        await _framework(spawner).run_exchange(
            system_prompt="",
            user_message="hi",
            llm=_BareHandle(),
            tools=[],
            tool_bridge=bridge,
        )
    # Recoverable — the engine's retry loop re-spawns on the same bridge.
    assert excinfo.value.level.name == "RECOVERABLE"
    # Redaction: the child's stdout never reaches the error surface.
    assert "boom" not in json.dumps(excinfo.value.to_dict(), default=str)


async def test_subprocess_framework_unparseable_stdout_maps_server_error(
    bridge: ToolBridgeServer,
) -> None:
    spawner = _CaptureSpawner(_StubProc(stdout=b"this is not json"))
    with pytest.raises(ModelServerError):
        await _framework(spawner).run_exchange(
            system_prompt="",
            user_message="hi",
            llm=_BareHandle(),
            tools=[],
            tool_bridge=bridge,
        )


async def test_subprocess_framework_not_logged_in_maps_fatal_auth(
    bridge: ToolBridgeServer,
) -> None:
    stdout = json.dumps(
        {
            "is_error": True,
            "subtype": "error",
            "result": "Not logged in · Please run /login",
        }
    ).encode()
    spawner = _CaptureSpawner(_StubProc(stdout=stdout))
    with pytest.raises(ModelAuthenticationFailed) as excinfo:
        await _framework(spawner).run_exchange(
            system_prompt="",
            user_message="hi",
            llm=_HandleWithSpec(),
            tools=[],
            tool_bridge=bridge,
        )
    assert excinfo.value.level.name == "FATAL"


# ---------------------------------------------------------------------------
# Test 5 — cancel kills the child and reaps it; clean surface.
# ---------------------------------------------------------------------------


async def test_subprocess_framework_cancel_kills_and_reaps(
    bridge: ToolBridgeServer,
) -> None:
    proc = _StubProc(stdout=b"", block_until_killed=True)
    spawner = _CaptureSpawner(proc)
    token = CancelToken()

    run = asyncio.create_task(
        _framework(spawner).run_exchange(
            system_prompt="",
            user_message="hang forever",
            llm=_HandleWithSpec(),
            tools=[],
            cancel_token=token,
            tool_bridge=bridge,
        )
    )
    # Let the spawn happen, then fire the cancel token (Esc).
    for _ in range(50):
        if spawner.argv is not None:
            break
        await asyncio.sleep(0.01)
    assert spawner.argv is not None, "run_exchange never spawned"
    assert proc.kill_calls == 0
    token.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, timeout=5)
    # The child was killed AND reaped — no zombie, no orphan.
    assert proc.kill_calls == 1
    assert proc.reaped is True


async def test_subprocess_framework_ambient_cancel_also_kills(
    bridge: ToolBridgeServer,
) -> None:
    """A hard task-cancel (workflow node timeout) — not the token —
    still kills + reaps the child on the way out."""
    proc = _StubProc(stdout=b"", block_until_killed=True)
    spawner = _CaptureSpawner(proc)

    run = asyncio.create_task(
        _framework(spawner).run_exchange(
            system_prompt="",
            user_message="hang forever",
            llm=_HandleWithSpec(),
            tools=[],
            cancel_token=CancelToken(),
            tool_bridge=bridge,
        )
    )
    for _ in range(50):
        if spawner.argv is not None:
            break
        await asyncio.sleep(0.01)
    assert spawner.argv is not None
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, timeout=5)
    assert proc.kill_calls == 1
    assert proc.reaped is True
