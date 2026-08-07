"""Component tests for src/tools — TC-TLS-01..08.

Spec: docs/explanation/tools-subsystem.md + Module API Reference §9.
Tests use real provider implementations (DotenvSecretsProvider,
NativeSandboxProvider, RuleBasedAuthorizationProvider) with stubs
only at the MCP-server boundary (real MCP wire-protocol is deferred).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from pydantic import BaseModel

from errors import (
    SecretNotFound,
    ToolExecutionFailed,
    ToolInvalidArguments,
    ToolSetupFailed,
    ToolUnauthorized,
)
from files import PathPolicy
from providers import (
    DotenvSecretsProvider,
    NativeSandboxProvider,
    RuleBasedAuthorizationProvider,
)
from schemas import AgentId, SessionId, ThreadId
from tools import (
    MCPServer,
    ToolContext,
    ToolsSubsystem,
    register_tool,
    tool,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _root(tmp_path: Path) -> Path:
    for sub in ("data", "bundles", ".platform", "logs"):
        (tmp_path / sub).mkdir(exist_ok=True)
    return tmp_path


def _policy() -> PathPolicy:
    return PathPolicy(
        allowed_paths=["data/"],
        denied_paths=[".env"],
    )


def _write_env(tmp_path: Path, payload: dict[str, str]) -> None:
    lines = [f"{k}={v}" for k, v in payload.items()]
    (tmp_path / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _new_context(*, agent_id: str = "ag_test") -> ToolContext:
    return ToolContext(
        agent_id=AgentId(agent_id),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal=f"agent.{agent_id}",
        secrets={},
    )


def _allow_all_authz() -> RuleBasedAuthorizationProvider:
    return RuleBasedAuthorizationProvider(
        config={"rules": [{"who": ".*", "what": ".*", "effect": "allow"}]}
    )


def _make_subsystem(
    *,
    tmp_path: Path,
    authz: RuleBasedAuthorizationProvider | None = None,
    env: dict[str, str] | None = None,
) -> ToolsSubsystem:
    root = _root(tmp_path)
    _write_env(tmp_path, env or {})
    return ToolsSubsystem(
        secrets=DotenvSecretsProvider(root=root, config={"path": ".env"}),
        sandbox=NativeSandboxProvider(),
        authz=authz or _allow_all_authz(),
        path_policy=_policy(),
        root=root,
    )


# ===========================================================================
# TC-TLS-01 — dispatch order: validate → authz → secrets → sandbox → execute
# ===========================================================================


async def test_dispatch_order_is_validate_authz_secrets_sandbox_execute(
    tmp_path: Path,
) -> None:
    order: list[str] = []

    class ArgsModel(BaseModel):
        n: int

        def model_post_init(self, _ctx: Any) -> None:
            order.append("validate")

    @tool(name="recorder", description="records call order", args_schema=ArgsModel)
    async def recorder(args: ArgsModel, ctx: ToolContext) -> dict[str, int]:
        order.append("execute")
        # ctx.secrets is populated AFTER step 3 — observe it here as proof.
        assert "K" in ctx.secrets
        return {"value": args.n}

    class _RecordingAuthz:
        async def allow(
            self,
            principal: str,
            action: str,
            *,
            target: str | None = None,
            callback: Any = None,
            layers: Any = (),
            caller_chain: Any = (),
            tool_call_id: str | None = None,
        ) -> Any:
            order.append("authz")
            return await _allow_all_authz().allow(
                principal,
                action,
                target=target,
                callback=callback,
                layers=layers,
                caller_chain=caller_chain,
                tool_call_id=tool_call_id,
            )

        async def list_rules(self) -> list[Any]:  # pragma: no cover
            return []

    class _RecordingSecrets:
        def __init__(self) -> None:
            self._inner = DotenvSecretsProvider(
                root=_root(tmp_path), config={"path": ".env"}
            )

        async def resolve(self, name: str) -> str:
            order.append("secrets")
            return await self._inner.resolve(name)

        async def exists(self, name: str) -> bool:
            return await self._inner.exists(name)

        async def list_names(self) -> list[str]:
            return await self._inner.list_names()

    class _RecordingSandbox:
        async def execute(
            self,
            work: Any,
            policy: dict[str, Any],
        ) -> Any:
            order.append("sandbox")
            return await work()

    _write_env(tmp_path, {"K": "v"})
    subsystem = ToolsSubsystem(
        secrets=_RecordingSecrets(),
        sandbox=_RecordingSandbox(),
        authz=_RecordingAuthz(),
        path_policy=_policy(),
        root=_root(tmp_path),
    )
    # Register the tool so secrets_needed is declared.
    register_tool(
        recorder,
        secrets_needed=["K"],
    )
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_test"),
        tools=[recorder],
    )

    ctx = _new_context()
    await subsystem.invoke_tool("recorder", {"n": 1}, ctx)

    # Module API Reference §9.1 + Step 10 prompt: EXACT order is
    # validate (Pydantic) → authz → secrets → sandbox → execute.
    # (Spec §6.2 lists authz-first in prose; the arbiter says
    # validate-first. The implementation honours the arbiter.)
    assert order == ["validate", "authz", "secrets", "sandbox", "execute"], (
        f"unexpected dispatch order: {order}"
    )


# ===========================================================================
# TC-TLS-02 — bad args rejected before tool runs
# ===========================================================================


async def test_bad_args_raise_invalid_arguments_and_tool_never_runs(
    tmp_path: Path,
) -> None:
    ran = False

    class StrictArgs(BaseModel):
        path: str
        encoding: str = "utf-8"

    @tool(name="strict", description="strict args tool", args_schema=StrictArgs)
    async def strict(args: StrictArgs, ctx: ToolContext) -> str:
        nonlocal ran
        ran = True
        return args.path

    subsystem = _make_subsystem(tmp_path=tmp_path)
    await subsystem.register_for_agent(agent_id=AgentId("ag_test"), tools=[strict])

    with pytest.raises(ToolInvalidArguments) as exc_info:
        await subsystem.invoke_tool(
            "strict",
            {"encoding": "utf-8"},
            _new_context(),  # missing 'path'
        )
    assert exc_info.value.detail["tool_name"] == "strict"
    assert ran is False


# ===========================================================================
# TC-TLS-03 — authz denial surfaces as Diagnostic
# ===========================================================================


async def test_authz_denial_raises_tool_unauthorized_diagnostic(
    tmp_path: Path,
) -> None:
    class _Args(BaseModel):
        x: int

    @tool(name="deniable", description="x", args_schema=_Args)
    async def deniable(args: _Args, ctx: ToolContext) -> int:
        return args.x

    deny_bash = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"who": ".*", "what": r"tool\.deniable", "effect": "deny"},
                {"who": ".*", "what": ".*", "effect": "allow"},
            ]
        }
    )
    subsystem = _make_subsystem(tmp_path=tmp_path, authz=deny_bash)
    await subsystem.register_for_agent(agent_id=AgentId("ag_test"), tools=[deniable])

    with pytest.raises(ToolUnauthorized) as exc_info:
        await subsystem.invoke_tool("deniable", {"x": 1}, _new_context())
    assert exc_info.value.detail["action"] == "tool.deniable"


# ===========================================================================
# TC-TLS-04 — tool exception → tool.execution_failed (Diagnostic)
# ===========================================================================


async def test_tool_exception_becomes_tool_execution_failed(
    tmp_path: Path,
) -> None:
    class _Args(BaseModel):
        x: int

    class _MyBoom(RuntimeError):
        pass

    @tool(name="exploder", description="raises", args_schema=_Args)
    async def exploder(args: _Args, ctx: ToolContext) -> int:
        msg = "kaboom"
        raise _MyBoom(msg)

    subsystem = _make_subsystem(tmp_path=tmp_path)
    await subsystem.register_for_agent(agent_id=AgentId("ag_test"), tools=[exploder])

    with pytest.raises(ToolExecutionFailed) as exc_info:
        await subsystem.invoke_tool("exploder", {"x": 1}, _new_context())
    assert exc_info.value.detail["error_class"] == "_MyBoom"
    # The original exception is preserved in __cause__ for in-process debugging.
    assert isinstance(exc_info.value.__cause__, _MyBoom)


# ===========================================================================
# TC-TLS-05 — missing secret → tool.setup_failed with secret.not_found cause
# ===========================================================================


async def test_missing_secret_fails_registration_with_chained_cause(
    tmp_path: Path,
) -> None:
    """Spec §6.1 step 3 — registration verifies secrets via
    `secrets.exists`. A tool declaring an absent secret fails to
    register with ToolSetupFailed; the cause chain carries
    SecretNotFound so the operator sees the underlying reason."""

    class _Args(BaseModel):
        q: str

    @tool(name="needy", description="needs a secret", args_schema=_Args)
    async def needy(args: _Args, ctx: ToolContext) -> str:
        return ctx.secrets["MISSING_KEY"]

    register_tool(needy, secrets_needed=["MISSING_KEY"])

    # Empty .env — MISSING_KEY is absent.
    subsystem = _make_subsystem(tmp_path=tmp_path, env={})

    with pytest.raises(ToolSetupFailed) as exc_info:
        await subsystem.register_for_agent(agent_id=AgentId("ag_test"), tools=[needy])
    assert exc_info.value.detail["tool_name"] == "needy"
    assert isinstance(exc_info.value.__cause__, SecretNotFound)


# ===========================================================================
# TC-TLS-06 — verbatim result capture (no transformation)
# ===========================================================================


async def test_verbatim_result_capture(tmp_path: Path) -> None:
    class _Args(BaseModel):
        n: int

    payload = {
        "answer": 42,
        "nested": [1, 2, {"deep": "value"}],
        "unicode": "café",
    }

    @tool(name="structured", description="returns structured value", args_schema=_Args)
    async def structured(args: _Args, ctx: ToolContext) -> dict[str, Any]:
        return payload

    subsystem = _make_subsystem(tmp_path=tmp_path)
    await subsystem.register_for_agent(agent_id=AgentId("ag_test"), tools=[structured])

    result = await subsystem.invoke_tool("structured", {"n": 1}, _new_context())
    assert result.result == payload
    # Same object reference too (passthrough, not a copy).
    assert result.result is payload


# ===========================================================================
# TC-TLS-07 — read honors path policy; bash exempt (v1 documented gap)
# ===========================================================================


async def test_builtin_read_honors_path_policy(tmp_path: Path) -> None:
    from errors import FileAccessDenied

    subsystem = _make_subsystem(tmp_path=tmp_path)
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_test"),
        builtin_names=["read"],
    )

    # Allowed: data/x.txt under the policy's data/ prefix.
    (tmp_path / "data" / "x.txt").write_text("hello", encoding="utf-8")
    result = await subsystem.invoke_tool("read", {"path": "data/x.txt"}, _new_context())
    assert result.result["content"] == "hello"

    # Denied: .env is in denied_paths.
    with pytest.raises(FileAccessDenied):
        await subsystem.invoke_tool("read", {"path": ".env"}, _new_context())


async def test_builtin_write_honors_path_policy(tmp_path: Path) -> None:
    """Path-policy enforcement on `write` — symmetric coverage with
    `read`. Allowed succeeds; denied raises FileAccessDenied."""
    from errors import FileAccessDenied

    subsystem = _make_subsystem(tmp_path=tmp_path)
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_test"),
        builtin_names=["write"],
    )
    # Allowed.
    await subsystem.invoke_tool(
        "write",
        {"path": "data/out.txt", "content": "hello"},
        _new_context(),
    )
    assert (tmp_path / "data" / "out.txt").read_text() == "hello"
    # Denied (.env is in denied_paths).
    with pytest.raises(FileAccessDenied):
        await subsystem.invoke_tool(
            "write",
            {"path": ".env", "content": "X=y"},
            _new_context(),
        )


async def test_builtin_edit_honors_path_policy(tmp_path: Path) -> None:
    """Path-policy enforcement on `edit` — symmetric coverage."""
    from errors import FileAccessDenied

    subsystem = _make_subsystem(tmp_path=tmp_path)
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_test"),
        builtin_names=["edit"],
    )
    # Allowed.
    (tmp_path / "data" / "x.txt").write_text("hello", encoding="utf-8")
    await subsystem.invoke_tool(
        "edit",
        {"path": "data/x.txt", "old_str": "hello", "new_str": "hi"},
        _new_context(),
    )
    assert (tmp_path / "data" / "x.txt").read_text() == "hi"
    # Denied (.env).
    with pytest.raises(FileAccessDenied):
        await subsystem.invoke_tool(
            "edit",
            {"path": ".env", "old_str": "X", "new_str": "Y"},
            _new_context(),
        )


async def test_write_into_content_dir_is_refused_even_when_policy_allows(
    tmp_path: Path,
) -> None:
    """Spec Fh-I5 + Fh-I9 — tools MUST NOT write to ``content/``
    even when the path policy allows reads there. The default
    platform.yaml policy puts ``content/`` in ``allowed_paths``
    (spec §4.4), so without the write-side guard a
    ``write({"path": "content/x.bin"})`` would smuggle into the
    engine's sidecar store. Pin the second gate."""
    from errors import FileAccessDenied

    root = _root(tmp_path)
    (root / "content").mkdir(exist_ok=True)
    permissive_policy = PathPolicy(
        allowed_paths=["data/", "content/"],  # reads under content/ allowed
        denied_paths=[],
    )
    _write_env(tmp_path, {})
    subsystem = ToolsSubsystem(
        secrets=DotenvSecretsProvider(root=root, config={"path": ".env"}),
        sandbox=NativeSandboxProvider(),
        authz=_allow_all_authz(),
        path_policy=permissive_policy,
        root=root,
    )
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_test"),
        builtin_names=["write", "edit"],
    )

    # The path-policy check PASSES (content/ is allowed for reads).
    # The write-side guard MUST refuse.
    with pytest.raises(FileAccessDenied):
        await subsystem.invoke_tool(
            "write",
            {"path": "content/forbidden.bin", "content": "x"},
            _new_context(),
        )

    # Edit too — it reads AND writes.
    (root / "content" / "existing.txt").write_text("old", encoding="utf-8")
    with pytest.raises(FileAccessDenied):
        await subsystem.invoke_tool(
            "edit",
            {
                "path": "content/existing.txt",
                "old_str": "old",
                "new_str": "new",
            },
            _new_context(),
        )


async def test_builtin_bash_bypasses_path_policy_v1_gap(tmp_path: Path) -> None:
    """Spec §4.7 + Fh-I6 — `bash` does NOT enforce the path policy
    in v1's native sandbox. Pin the documented limitation so a future
    real sandbox flips this test deliberately."""
    subsystem = _make_subsystem(tmp_path=tmp_path)
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_test"),
        builtin_names=["bash"],
    )

    # The bash command touches a denied path. The path policy is NOT
    # consulted; bash runs the command. v1 limitation.
    target = tmp_path / "denied_zone" / "x.txt"
    target.parent.mkdir()
    target.write_text("untouched", encoding="utf-8")
    result = await subsystem.invoke_tool(
        "bash",
        {"command": f"cat {target}"},
        _new_context(),
    )
    assert result.result["stdout"].strip() == "untouched"
    assert result.result["exit_code"] == 0


# ===========================================================================
# TC-TLS-08 — lazy MCP server starts on first use
# ===========================================================================


class _StubMCPServer:
    """An in-memory MCPServer for testing the lazy-start lifecycle.

    Real MCP wire-protocol (subprocess + JSON-RPC over stdio) is
    deferred. This stub honours the same Protocol so tests can pin
    the lifecycle without spinning up a real subprocess.
    """

    name: str
    started: bool
    start_count: int
    invoke_count: int

    def __init__(self, name: str, tools: dict[str, Any]) -> None:
        self.name = name
        self._tools = tools
        self.started = False
        self.start_count = 0
        self.invoke_count = 0

    async def start(self) -> None:
        self.started = True
        self.start_count += 1

    async def stop(self) -> None:
        self.started = False

    async def list_tools(self) -> list[dict[str, Any]]:
        return [{"name": k, "description": d} for k, d in self._tools.items()]

    async def invoke(self, tool_name: str, args: dict[str, Any]) -> Any:
        self.invoke_count += 1
        return self._tools[tool_name]


async def test_lazy_mcp_server_starts_on_first_invoke(tmp_path: Path) -> None:
    server = _StubMCPServer(
        name="stub",
        tools={"hello": "from stub"},
    )
    subsystem = _make_subsystem(tmp_path=tmp_path)
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_test"),
        mcp_servers=[(server, "lazy")],
    )
    # Lazy — server is NOT started at registration time. We assert
    # via the call-count side channel rather than `assert server.started`
    # so mypy's flow-narrowing of bool literal asserts doesn't mark
    # later assertions as unreachable.
    assert server.start_count == 0

    result = await subsystem.invoke_tool("mcp.stub.hello", {}, _new_context())
    # First invoke triggers start.
    assert server.start_count == 1
    assert server.invoke_count == 1
    assert result.result == "from stub"

    # Second invoke reuses; start NOT called again.
    await subsystem.invoke_tool("mcp.stub.hello", {}, _new_context())
    assert server.start_count == 1
    assert server.invoke_count == 2


async def test_eager_mcp_server_starts_at_registration(tmp_path: Path) -> None:
    server = _StubMCPServer(name="eager_stub", tools={"ping": "pong"})
    subsystem = _make_subsystem(tmp_path=tmp_path)
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_test"),
        mcp_servers=[(server, "eager")],
    )
    assert server.started
    assert server.start_count == 1


# ===========================================================================
# Spec invariants — additional coverage
# ===========================================================================


async def test_unknown_tool_raises(tmp_path: Path) -> None:
    from errors import ToolNotFound

    subsystem = _make_subsystem(tmp_path=tmp_path)
    await subsystem.register_for_agent(agent_id=AgentId("ag_test"))
    with pytest.raises(ToolNotFound):
        await subsystem.invoke_tool("does_not_exist", {}, _new_context())


def test_mcp_server_protocol_shape() -> None:
    """The MCPServer Protocol is structural; the stub satisfies it."""
    assert isinstance(_StubMCPServer(name="x", tools={}), MCPServer)


# ===========================================================================
# Layer 3 validator — TC-TLS adjacent (spec §3.4)
# ===========================================================================


def test_layer3_accepts_default_builtin_set() -> None:
    from tools import validate_standard_config

    result = validate_standard_config({"builtin": ["read", "write", "edit", "bash"]})
    assert result.ok is True, result.errors


def test_layer3_rejects_unknown_builtin() -> None:
    from tools import validate_standard_config

    result = validate_standard_config({"builtin": ["read", "frobnicate"]})
    assert result.ok is False
    codes = [e.code for e in result.errors]
    assert "unknown_builtin" in codes


def test_layer3_rejects_duplicate_mcp_server_name() -> None:
    from tools import validate_standard_config

    result = validate_standard_config(
        {
            "mcp": [
                {"name": "github", "command": ["x"]},
                {"name": "github", "command": ["y"]},
            ]
        }
    )
    assert result.ok is False
    assert any(e.code == "duplicate_mcp_name" for e in result.errors)


def test_layer3_rejects_env_inject_referencing_undeclared_secret() -> None:
    from tools import validate_standard_config

    result = validate_standard_config(
        {
            "mcp": [
                {
                    "name": "github",
                    "command": ["x"],
                    "secrets_needed": ["GITHUB_TOKEN"],
                    "env_inject": {"GITHUB_PAT": "WRONG_NAME"},
                }
            ]
        }
    )
    assert result.ok is False
    assert any(e.code == "unknown_secret_in_env_inject" for e in result.errors)


async def test_declared_from_file_discovery_and_registration(
    tmp_path: Path,
) -> None:
    """Spec §6.1 step 1 — the Tools subsystem (NOT Asset Management)
    imports declared .py files at registration time and registers
    every ``@tool``-decorated function it finds. Pin the contract:

    - The Python file is imported (Am-I10 forbids this from happening
      in Asset Management).
    - Each ``@tool``-decorated function is registered.
    - Non-tool callables are ignored.
    - The tool is invocable through the same dispatch flow as any
      other tool.
    """
    bundle = tmp_path / "bundles" / "demo" / "tools"
    bundle.mkdir(parents=True)
    source = '''
from pydantic import BaseModel
from tools import tool


class GreetArgs(BaseModel):
    name: str


@tool(name="greet", description="Greets the caller", args_schema=GreetArgs)
async def greet(args, ctx):
    return {"text": f"hi, {args.name}"}


def not_a_tool() -> None:
    """A bare helper that MUST NOT be registered."""
'''
    tool_file = bundle / "greet.py"
    tool_file.write_text(source, encoding="utf-8")

    subsystem = _make_subsystem(tmp_path=tmp_path)
    info = await subsystem.register_for_agent(
        agent_id=AgentId("ag_test"),
        declared_paths=[tool_file],
    )
    names = {i.name for i in info}
    assert names == {"greet"}, f"unexpected discovery: {names}"

    result = await subsystem.invoke_tool("greet", {"name": "world"}, _new_context())
    assert result.result == {"text": "hi, world"}


async def test_declared_from_file_missing_file_raises_setup_failed(
    tmp_path: Path,
) -> None:
    from errors import ToolSetupFailed

    subsystem = _make_subsystem(tmp_path=tmp_path)
    with pytest.raises(ToolSetupFailed):
        await subsystem.register_for_agent(
            agent_id=AgentId("ag_test"),
            declared_paths=[tmp_path / "does_not_exist.py"],
        )


async def test_declared_from_file_syntax_error_raises_setup_failed(
    tmp_path: Path,
) -> None:
    from errors import ToolSetupFailed

    bad = tmp_path / "bad.py"
    bad.write_text("def syntax error:\n", encoding="utf-8")
    subsystem = _make_subsystem(tmp_path=tmp_path)
    with pytest.raises(ToolSetupFailed):
        await subsystem.register_for_agent(
            agent_id=AgentId("ag_test"),
            declared_paths=[bad],
        )


def test_layer3_rejects_malformed_declared_entry() -> None:
    from tools import validate_standard_config

    result = validate_standard_config({"declared": ["just-a-string-not-a-mapping"]})
    assert result.ok is False
    assert any(e.code == "malformed_entry" for e in result.errors)


# ===========================================================================
# Step C follow-up (bug-engine-tools-wiring-gap.md) — pin args_schema shape
# ===========================================================================


async def test_declared_tool_info_carries_valid_json_schema(
    tmp_path: Path,
) -> None:
    """Step C of the bug fix imagined a ``BoundTool`` dataclass
    with a ``schema: dict`` field that the framework adapter
    forwards to the model's ``tool_use`` protocol. The equivalent
    in our architecture is ``ToolInfo.args_schema`` — derived from
    the tool function's Pydantic args model via
    ``model_json_schema()``.

    This test pins the load-bearing property CrewAI / litellm
    depends on: a declared tool's args_schema is a valid JSON
    Schema dict that names its parameters + types + required
    fields. Without this shape, the model gets a stringified
    Python signature and tool_use breaks.

    The Step C bug-doc prompt mentioned "schema derivation logic"
    as a potential escalation trigger; the @tool decorator's
    Pydantic-args contract sidesteps that — Pydantic's
    model_json_schema is the derivation, by-construction valid.
    This test pins the resulting shape so a future
    decorator refactor that drops the json-schema export would
    fail loudly rather than silently produce a stringified
    schema.
    """
    bundle = tmp_path / "bundles" / "demo" / "tools"
    bundle.mkdir(parents=True)
    source = """
from pydantic import BaseModel, Field
from tools import tool


class EchoArgs(BaseModel):
    message: str = Field(description="text to echo back")
    repeat: int = Field(default=1, ge=1, description="how many copies")


@tool(name="echo", description="Echo the message N times", args_schema=EchoArgs)
async def echo(args, ctx):
    return {"text": " ".join([args.message] * args.repeat)}
"""
    tool_file = bundle / "echo.py"
    tool_file.write_text(source, encoding="utf-8")

    subsystem = _make_subsystem(tmp_path=tmp_path)
    infos = await subsystem.register_for_agent(
        agent_id=AgentId("ag_test"),
        declared_paths=[tool_file],
    )
    info = next(i for i in infos if i.name == "echo")

    schema = info.args_schema
    # Shape: a valid JSON Schema dict.
    assert isinstance(schema, dict)
    assert schema.get("type") == "object"
    # Both fields surface; their types are named.
    properties = schema.get("properties", {})
    assert "message" in properties
    assert "repeat" in properties
    assert properties["message"].get("type") == "string"
    assert properties["repeat"].get("type") == "integer"
    # `required` reflects the non-defaulted field.
    required = schema.get("required", [])
    assert "message" in required
    assert "repeat" not in required  # has default
    # Description carries through — the model uses it to decide
    # how to fill the parameter.
    assert "echo" in properties["message"].get("description", "").lower() or (
        properties["message"].get("description") == "text to echo back"
    )


# ---------------------------------------------------------------------------
# Regression: bug-authz-rule-caller-bundle-mismatch
# ---------------------------------------------------------------------------
#
# Butler agent (`acme` bundle) extends `kolsch/agents/helper`. Helper's
# tools config lists them as `declared:` refs like
# `kolsch/tools/read_file`. Before the fix, the engine passed the CALLER
# agent's bundle (`acme`) as `home_bundle=` to `register_for_agent` for
# ALL declared tools, so `defn.action_name` returned
# `acme/tools/read_file`. Modal-granted rules then landed under `acme/`
# and boot on today's main fails with `AuthzToolRefUnknown` because
# the ref doesn't resolve to a catalogued tool.
#
# The fix threads `declared_owner_bundles: dict[Path, str]` through
# `register_for_agent`. The map is derived from each declared ref's
# leading segment at engine setup time. Per-path owner takes precedence
# over the caller-supplied `home_bundle=` default.


async def test_declared_owner_bundles_overrides_home_bundle(
    tmp_path: Path,
) -> None:
    """Declared owner map wins over `home_bundle=` for declared tools.

    Regression for bug-authz-rule-caller-bundle-mismatch. Simulates a
    butler (`ag_butler` in `acme` bundle) inheriting a
    `kolsch/tools/greet` declared ref via
    ``extends: kolsch/agents/helper``. The tool file lives under
    ``bundles/kolsch/tools/`` on disk. Owner-bundle map says the tool
    belongs to ``kolsch``; the ``home_bundle`` kwarg says ``acme``
    (caller's bundle). The map must win.
    """
    bundle = tmp_path / "bundles" / "kolsch" / "tools"
    bundle.mkdir(parents=True)
    source = """
from pydantic import BaseModel
from tools import tool


class GreetArgs(BaseModel):
    name: str


@tool(name="greet", description="Greets the caller", args_schema=GreetArgs)
async def greet(args, ctx):
    return {"text": f"hi, {args.name}"}
"""
    tool_file = bundle / "greet.py"
    tool_file.write_text(source, encoding="utf-8")

    subsystem = _make_subsystem(tmp_path=tmp_path)
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_butler"),
        declared_paths=[tool_file],
        # The map is what the fixed engine passes for declared refs.
        declared_owner_bundles={tool_file: "kolsch"},
        # The caller-derived default that USED to leak into every
        # declared tool's home_bundle. Must be overridden by the map.
        home_bundle="acme",
    )
    defns = subsystem._registry.list_tools(AgentId("ag_butler"))
    greet_defn = next(d for d in defns if d.name == "greet")
    assert greet_defn.home_bundle == "kolsch", (
        "Declared owner map must take precedence over the caller's "
        "home_bundle default — bug-authz-rule-caller-bundle-mismatch."
    )
    assert greet_defn.action_name == "kolsch/tools/greet", (
        "action_name must resolve against the TOOL's own bundle, not "
        "the caller agent's bundle. Modal-granted rules key off this."
    )


async def test_declared_paths_without_owner_map_uses_home_bundle_fallback(
    tmp_path: Path,
) -> None:
    """Backward-compat: callers that omit the map fall through to `home_bundle=`.

    Programmatic / test callers that pass only `declared_paths=[...]`
    (no owner map) keep the pre-fix behaviour — every declared tool
    gets stamped with the caller-supplied `home_bundle=` value. This
    is what the 4 existing test callers rely on; the migration path
    for `register_for_agent`'s signature must not break them.
    """
    bundle = tmp_path / "bundles" / "programmatic" / "tools"
    bundle.mkdir(parents=True)
    source = """
from pydantic import BaseModel
from tools import tool


class NoopArgs(BaseModel):
    pass


@tool(name="noop", description="does nothing", args_schema=NoopArgs)
async def noop(args, ctx):
    return {"ok": True}
"""
    tool_file = bundle / "noop.py"
    tool_file.write_text(source, encoding="utf-8")

    subsystem = _make_subsystem(tmp_path=tmp_path)
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_test"),
        declared_paths=[tool_file],
        # NO declared_owner_bundles — mimics the 4 existing test
        # callers that pass raw Paths.
        home_bundle="myprogram",
    )
    defns = subsystem._registry.list_tools(AgentId("ag_test"))
    noop_defn = next(d for d in defns if d.name == "noop")
    assert noop_defn.home_bundle == "myprogram", (
        "Backward compat: when declared_owner_bundles is None, the "
        "caller-supplied home_bundle= still stamps each declared tool."
    )
