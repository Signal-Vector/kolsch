"""feat-160 — every enforcement caller honours locked platform rules.

The locked stage lives inside the provider, so it must cover every path
that reaches `allow()`: `dispatch_tool` with each caller layer (the
workflow / agent / bundle widening probe), `ToolsSubsystem._invoke_mcp`,
the in-tool `tools.permissions.check`, and a real workflow builtin node
run through the executor. Each case is allowed without the lock and
denied with it.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import BaseModel

from assets.catalog import AssetCatalog, AssetEntry
from assets.manifest import BundleManifest
from errors import ToolUnauthorized
from providers import (
    LOCKED_RULE_ID_PREFIX,
    AuthzEffect,
    Rule,
    RuleBasedAuthorizationProvider,
    derive_locked_rule_id,
)
from providers.sandbox import NativeSandboxProvider
from providers.secrets import DotenvSecretsProvider
from tests.fixtures.assertions import assert_event_content, assert_event_sequence
from tools import permissions
from tools.decorator import ToolContext, get_tool_definition, tool
from tools.dispatch import dispatch_tool
from tools.subsystem import ToolsSubsystem
from workflows.builtins import register_builtins
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

_BASH = "kolsch/tools/bash"
_LOCK_BASH = {"what": _BASH, "pattern": "*", "effect": "deny"}
_ALLOW_BASH = [Rule(what=_BASH, pattern="*", effect="allow")]
# Platform: bash explicitly denied (the probe's P2 shape), tail present.
_PLATFORM_DENY_BASH: list[dict[str, Any]] = [
    {"what": _BASH, "pattern": "*", "effect": "deny"},
    {"what": "*", "pattern": "*", "effect": "deny"},
]


def _authz(
    *, locked: bool, rules: list[dict[str, Any]] | None = None
) -> RuleBasedAuthorizationProvider:
    config: dict[str, Any] = {
        "rules": rules if rules is not None else _PLATFORM_DENY_BASH
    }
    if locked:
        config["locked_rules"] = [_LOCK_BASH]
    return RuleBasedAuthorizationProvider(config=config)


def _assert_locked(exc: ToolUnauthorized) -> None:
    matched = exc.detail.get("matched_rule")
    assert isinstance(matched, dict)
    assert matched["id"] == derive_locked_rule_id(_BASH, "*", "deny")


# ---------------------------------------------------------------------------
# dispatch_tool — the s7 probe matrix as a test
# ---------------------------------------------------------------------------


class _Args(BaseModel):
    command: str


@tool(name="bash", description="fake", args_schema=_Args, action=_BASH)
async def _fake_bash(args: _Args, ctx: ToolContext) -> str:
    return "RAN:" + args.command


class _NoSecrets:
    async def resolve(self, name: str) -> str:
        raise KeyError(name)


def _ctx() -> ToolContext:
    return ToolContext(
        agent_id="ag_x",
        session_id=f"ses_{uuid.uuid4()}",
        thread_id=f"th_{uuid.uuid4()}",
        principal="user.local",
        secrets={},
        workspace_root=None,
        path_policy=None,
        tool_call_id=None,
    )


async def _dispatch(authz: RuleBasedAuthorizationProvider, **layers: Any) -> Any:
    return await dispatch_tool(
        defn=get_tool_definition(_fake_bash),
        args={"command": "rm -rf /tmp/x"},
        context=_ctx(),
        authz=authz,
        secrets=cast("Any", _NoSecrets()),
        sandbox=NativeSandboxProvider(),
        **layers,
    )


@pytest.mark.parametrize(
    "layer_kw", ["workflow_permissions", "agent_permissions", "bundle_permissions"]
)
async def test_dispatch_layer_allow_widens_without_lock_and_loses_to_it(
    layer_kw: str,
) -> None:
    # Without the lock: the layer allow overrides the platform deny.
    assert await _dispatch(_authz(locked=False), **{layer_kw: _ALLOW_BASH}) == (
        "RAN:rm -rf /tmp/x"
    )
    # With the lock: denied, by the locked rule.
    with pytest.raises(ToolUnauthorized) as excinfo:
        await _dispatch(_authz(locked=True), **{layer_kw: _ALLOW_BASH})
    _assert_locked(excinfo.value)


async def test_dispatch_lock_beats_platform_allow_and_all_layers() -> None:
    authz = _authz(
        locked=True,
        rules=[{"what": _BASH, "pattern": "*", "effect": "allow"}],
    )
    with pytest.raises(ToolUnauthorized) as excinfo:
        await _dispatch(
            authz,
            workflow_permissions=_ALLOW_BASH,
            agent_permissions=_ALLOW_BASH,
            bundle_permissions=_ALLOW_BASH,
        )
    _assert_locked(excinfo.value)


# ---------------------------------------------------------------------------
# ToolsSubsystem._invoke_mcp
# ---------------------------------------------------------------------------


class _FakeMcpServer:
    name = "srv"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def start(self) -> None:
        return None

    async def invoke(self, tool_name: str, args: dict[str, Any]) -> str:
        self.calls.append(tool_name)
        return "mcp-ok"


@pytest.mark.parametrize("locked", [False, True])
async def test_invoke_mcp_agent_allow_vs_locked_namespace(*, locked: bool) -> None:
    config: dict[str, Any] = {
        "rules": [{"what": "*", "pattern": "*", "effect": "deny"}]
    }
    if locked:
        config["locked_rules"] = [{"what": "mcp.srv.*", "effect": "deny"}]
    authz = RuleBasedAuthorizationProvider(config=config)
    subsystem = ToolsSubsystem(
        secrets=cast("Any", _NoSecrets()),
        sandbox=NativeSandboxProvider(),
        authz=authz,
    )
    server = _FakeMcpServer()
    reg = SimpleNamespace(server=server, started=False)
    call = subsystem._invoke_mcp(
        (cast("Any", reg), "lookup"),
        "mcp.srv.lookup",
        {},
        _ctx(),
        agent_permissions=[Rule(what="mcp.srv.*", effect="allow")],
    )
    if not locked:
        result = await call
        assert result.result == "mcp-ok"
        return
    with pytest.raises(ToolUnauthorized) as excinfo:
        await call
    matched = excinfo.value.detail["matched_rule"]
    assert matched["id"].startswith(LOCKED_RULE_ID_PREFIX)
    assert server.calls == []


# ---------------------------------------------------------------------------
# tools.permissions.check (in-tool API)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("locked", "expected"), [(False, "allow"), (True, "deny")])
async def test_in_tool_check_agent_allow_vs_lock(
    *, locked: bool, expected: str
) -> None:
    binding = permissions.PermissionsBinding(
        provider=_authz(locked=locked),
        agent_permissions=list(_ALLOW_BASH),
        principal="agent.ag_helper",
        tool="bash",
    )
    token = permissions.bind(binding)
    try:
        effect = await permissions.check(_BASH)
    finally:
        permissions.reset(token)
    assert effect is AuthzEffect(expected)


# ---------------------------------------------------------------------------
# A real workflow builtin node through the executor
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bash_defn() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_test_feat160_bash_tool",
        Path(__file__).resolve().parents[3]
        / "src"
        / "cli"
        / "_starter_bundle"
        / "kolsch"
        / "tools"
        / "bash.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    defn = get_tool_definition(module.bash)
    defn.home_bundle = "kolsch"
    return defn


def _bash_catalog() -> AssetCatalog:
    return AssetCatalog(
        bundles={
            "kolsch": BundleManifest(
                name="kolsch",
                version="0.1.0",
                description="test fixture",
                path="/tmp/bundles/kolsch",
            )
        },
        assets={
            _BASH: AssetEntry(
                ref=_BASH,
                asset_type="tool",
                bundle="kolsch",
                path="bundles/kolsch/tools/bash.py",
            )
        },
    )


async def _run_bash_workflow(
    tmp_path: Path, bash_defn: Any, *, locked: bool
) -> tuple[WorkflowRunStatus, list[dict[str, Any]]]:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    config: dict[str, Any] = {
        "rules": _PLATFORM_DENY_BASH,
        # Emit every decision so the run log carries the locked one.
        "audit_level": "all",
    }
    if locked:
        config["locked_rules"] = [_LOCK_BASH]
    registry = RunnerRegistry()
    register_builtins(
        registry,
        bash_defn=bash_defn,
        authz=RuleBasedAuthorizationProvider(config=config, catalog=_bash_catalog()),
        secrets=DotenvSecretsProvider(root=tmp_path, config={"path": ".env"}),
        sandbox=NativeSandboxProvider(config={}),
    )
    workflow = Workflow.model_validate(
        {
            "nodes": {"greet": {"type": "kolsch/nodes/bash", "command": "echo hi"}},
            "policy": [{"what": _BASH, "pattern": "*", "effect": "allow"}],
            "graph": {"edges": []},
        }
    )
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    executor = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=_bash_catalog(),
    )
    status = await executor.run()
    return status, list(store.read_events(run.run_id))


def _as_typed(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    """Adapt the run log's `{kind, payload}` rows to the helpers' shape."""
    return [
        {"type": e["kind"], "data": e["payload"]} for e in events if e["kind"] == kind
    ]


async def test_workflow_policy_allow_succeeds_without_lock(
    tmp_path: Path, bash_defn: Any
) -> None:
    status, events = await _run_bash_workflow(tmp_path, bash_defn, locked=False)
    assert status == WorkflowRunStatus.SUCCEEDED, events[-3:]


async def test_workflow_node_denied_by_lock_despite_policy_allow(
    tmp_path: Path, bash_defn: Any
) -> None:
    status, events = await _run_bash_workflow(tmp_path, bash_defn, locked=True)
    assert status == WorkflowRunStatus.FAILED
    failed = _as_typed(events, "workflow.node.failed")
    assert_event_sequence(failed, ["workflow.node.failed"])
    assert "Authorization denied" in failed[0]["data"]["message"]
    decisions = _as_typed(events, "policy.decision.made")
    assert_event_sequence(decisions, ["policy.decision.made"])
    assert_event_content(
        decisions[0],
        action=_BASH,
        effect="deny",
        rule_id=derive_locked_rule_id(_BASH, "*", "deny"),
    )
