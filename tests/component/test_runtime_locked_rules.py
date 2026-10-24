"""feat-160 — locked platform rules through bootstrap, `/reload` and `/tools`.

`bootstrap_runtime` loads `authorization.config.locked_rules` with
source positions and fails loud (naming the file and the entry) on an
invalid list; `/reload` picks up an added lock and keeps the old
provider on an invalid one; `/tools` reports a locked tool as locked.
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
import yaml

from cli._slash import SlashContext, dispatch_slash
from cli.chat.services.authz_introspection import handle_reload
from errors import AuthzMalformedRule
from platform_runtime import bootstrap_runtime
from providers import LOCKED_RULE_ID_PREFIX, PermissionLayer, Rule

if TYPE_CHECKING:
    from pathlib import Path

_BASH = "kolsch/tools/bash"
_LOCK_BASH = {"what": _BASH, "pattern": "*", "effect": "deny"}


def _write_platform(root: Path, *, locked: object = None) -> None:
    from assets.platform_config import default_platform_payload

    payload = default_platform_payload()
    config: dict[str, Any] = {
        "rules": [
            {"what": "agent.execute.*", "pattern": "*", "effect": "allow"},
            {"what": _BASH, "pattern": "*", "effect": "allow"},
            {"what": "*", "pattern": "*", "effect": "deny"},
        ]
    }
    if locked is not None:
        config["locked_rules"] = locked
    payload["authorization"] = {"variant": "rule_based", "config": config}
    payload["defaults"] = {}
    (root / "platform.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )


def _workspace(root: Path, *, locked: object = None) -> Path:
    tools = root / "bundles" / "kolsch" / "tools"
    tools.mkdir(parents=True)
    (root / "bundles" / "kolsch" / "bundle.yaml").write_text(
        yaml.safe_dump(
            {"name": "kolsch", "version": "0.1.0", "description": "fixture"}
        ),
        encoding="utf-8",
    )
    (tools / "bash.py").write_text("# stub\n", encoding="utf-8")
    _write_platform(root, locked=locked)
    return root


def _line_of(root: Path, needle: str) -> int:
    lines = (root / "platform.yaml").read_text(encoding="utf-8").splitlines()
    in_locked = False
    for number, line in enumerate(lines, start=1):
        if line.strip().startswith("locked_rules:"):
            in_locked = True
        elif in_locked and needle in line:
            return number
    raise AssertionError(needle)


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------


async def test_bootstrap_loads_locked_rules_with_source(tmp_path: Path) -> None:
    root = _workspace(tmp_path, locked=[_LOCK_BASH])
    runtime = await bootstrap_runtime(root=root)
    [locked] = await runtime.authz.list_locked_rules()
    assert locked.id.startswith(LOCKED_RULE_ID_PREFIX)
    assert locked.source_file == str((root / "platform.yaml").resolve())
    assert locked.source_line == _line_of(root, "what: kolsch/tools/bash")
    # Locked deny beats the platform allow and an agent-layer allow.
    result = await runtime.authz.allow(
        "user.local",
        _BASH,
        target="ls",
        layers=(
            PermissionLayer(
                name="agent", rules=(Rule(what=_BASH, pattern="*", effect="allow"),)
            ),
        ),
    )
    assert result.allowed is False
    assert result.reason.endswith("(who_chain=locked)")


@pytest.mark.parametrize(
    ("locked", "needle"),
    [
        ([_LOCK_BASH, {"what": _BASH, "effect": "allow"}], "locked_rules[1]"),
        ([{"what": "kolsch/bash", "effect": "deny"}], "locked_rules[0]"),
        ("kolsch/tools/bash", "`locked_rules` in "),
    ],
)
async def test_bootstrap_invalid_locked_rules_fails_naming_file(
    tmp_path: Path, locked: object, needle: str
) -> None:
    root = _workspace(tmp_path, locked=locked)
    with pytest.raises(AuthzMalformedRule) as excinfo:
        await bootstrap_runtime(root=root)
    message = str(excinfo.value)
    assert needle in message
    assert "platform.yaml" in message


# ---------------------------------------------------------------------------
# /reload
# ---------------------------------------------------------------------------


async def test_reload_picks_up_added_lock(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    runtime = await bootstrap_runtime(root=root)
    before = await runtime.authz.allow("user.local", _BASH, target="ls")
    assert before.allowed is True
    _write_platform(root, locked=[_LOCK_BASH])
    result = await handle_reload(runtime)
    assert result.error is None, result.error
    after = await runtime.authz.allow("user.local", _BASH, target="ls")
    assert after.allowed is False
    assert after.reason.endswith("(who_chain=locked)")
    assert after.matched_rule is not None
    assert after.matched_rule.source_line == _line_of(root, "what: kolsch/tools/bash")


async def test_reload_invalid_lock_keeps_old_provider(tmp_path: Path) -> None:
    root = _workspace(tmp_path, locked=[_LOCK_BASH])
    runtime = await bootstrap_runtime(root=root)
    original = runtime.authz
    _write_platform(root, locked=[{"what": _BASH, "effect": "prompt"}])
    result = await handle_reload(runtime)
    assert result.summary is None
    assert result.error is not None
    assert "locked_rules[0]" in result.error
    assert "Old rules still active" in result.error
    assert runtime.authz is original
    assert len(await original.list_locked_rules()) == 1


# ---------------------------------------------------------------------------
# /tools
# ---------------------------------------------------------------------------


def _tools_ctx(runtime: Any, stdout: io.StringIO, tools: list[str]) -> SlashContext:
    config = SimpleNamespace(
        agent_id="ag_helper",
        subsystems={"tools": SimpleNamespace(config={"builtin": tools})},
    )
    return SlashContext(
        stdout=stdout, agent_config=cast("Any", config), authz=runtime.authz
    )


async def test_tools_reports_whole_tool_lock(tmp_path: Path) -> None:
    root = _workspace(tmp_path, locked=[_LOCK_BASH])
    runtime = await bootstrap_runtime(root=root)
    stdout = io.StringIO()
    await dispatch_slash("/tools", _tools_ctx(runtime, stdout, ["bash"]))
    [row] = [line for line in stdout.getvalue().splitlines() if "bash" in line]
    assert "(deny, locked)" in row
    assert LOCKED_RULE_ID_PREFIX in row


async def test_tools_annotates_pattern_lock_and_is_unchanged_without(
    tmp_path: Path,
) -> None:
    plain = _workspace(tmp_path / "plain")
    runtime = await bootstrap_runtime(root=plain)
    stdout = io.StringIO()
    await dispatch_slash("/tools", _tools_ctx(runtime, stdout, ["bash"]))
    plain_out = stdout.getvalue()
    assert "locked" not in plain_out

    locked_root = _workspace(
        tmp_path / "locked",
        locked=[{"what": _BASH, "pattern": "rm *", "effect": "deny"}],
    )
    runtime = await bootstrap_runtime(root=locked_root)
    stdout = io.StringIO()
    await dispatch_slash("/tools", _tools_ctx(runtime, stdout, ["bash"]))
    [row] = [line for line in stdout.getvalue().splitlines() if "bash" in line]
    [plain_row] = [line for line in plain_out.splitlines() if "bash" in line]
    assert row == plain_row + "  [locked deny for pattern 'rm *']"


# ---------------------------------------------------------------------------
# /reload rebinds the engine's provider (agent.execute.* pre-flight)
# ---------------------------------------------------------------------------


def _setup_workspace(root: Path) -> Path:
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if not k.startswith("KOLSCH_")}
    env["KOLSCH_WORKSPACE"] = str(root)
    result = subprocess.run(
        [sys.executable, "-m", "cli", "setup", "--non-interactive"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return root


def _set_locked(root: Path, locked: list[dict[str, Any]] | None) -> None:
    path = root / "platform.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = payload["authorization"]["config"]
    if locked:
        config["locked_rules"] = locked
    else:
        config.pop("locked_rules", None)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


async def _execute_helper(runtime: Any) -> Any:
    from engine import ExecutionContext

    config = await runtime.build_resolved_config("kolsch/agents/helper")
    session = await runtime.store.create_session(
        str(config.agent_id), name="feat160:preflight"
    )
    [thread] = await runtime.store.list_threads(session.session_id)
    ctx = ExecutionContext(
        thread_id=thread.thread_id,
        trigger={"kind": "user_message", "content": "hi"},
    )
    return await runtime.engine.execute(config, ctx)


async def test_reload_lock_and_unlock_reach_agent_preflight(tmp_path: Path) -> None:
    from engine.context import AgentExecutionError
    from errors import AgentUnauthorized

    root = _setup_workspace(tmp_path / "ws")
    runtime = await bootstrap_runtime(root=root)
    await _execute_helper(runtime)  # baseline: the pre-flight allows

    _set_locked(root, [{"what": "agent.execute.*", "effect": "deny"}])
    result = await handle_reload(runtime)
    assert result.error is None, result.error
    assert runtime.engine._authz is runtime.authz
    with pytest.raises(AgentExecutionError) as excinfo:
        await _execute_helper(runtime)
    # The pre-flight denial lands as the exchange's failed event.
    failed = excinfo.value.failed_event.model_dump(mode="json")
    error = failed["data"]["error"]
    assert error["type"] == AgentUnauthorized.code
    assert error["detail"]["matched_rule"]["id"].startswith(LOCKED_RULE_ID_PREFIX)

    _set_locked(root, None)
    result = await handle_reload(runtime)
    assert result.error is None, result.error
    await _execute_helper(runtime)  # unlocked again
