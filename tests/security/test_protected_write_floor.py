"""bug-161 — agent file tools can never write kolsch's control files.

`files.assert_writable_destination` carries a non-configurable
write floor (`files.PROTECTED_WRITE_PATHS`: `platform.yaml`,
`.kolsch/`, `.env`, `.platform/`). Every write-capable file tool
calls it from its body, AFTER authz, so no authz outcome (allow-all
rule set, a `bundles/*` allow that also matches `bundles/../x`,
`--auto-approve`) can reach those paths.

These tests drive the real `tools.dispatch.dispatch_tool` path with
a `RuleBasedAuthorizationProvider` built from explicit allow rules,
against the starter `write_file` tool loaded from an *installed*
workspace copy (`<root>/bundles/kolsch/tools/write_file.py`), plus
the `tools.builtins` factories and the workflow `write_file` node.

Every assertion is filesystem-independent: the floor compares
casefolded resolved paths, so `PLATFORM.YAML` is refused on
case-sensitive Linux (where it would be a different file) exactly as
on case-insensitive macOS APFS (where it IS `platform.yaml`). The
only host-dependent assertion is marked and skipped on
case-sensitive filesystems.
"""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from assets.catalog import AssetCatalog
from assets.platform_config import default_platform_payload
from cli._setup import _workspace_only_default_rules
from errors import FileAccessDenied
from files import PathPolicy, load_policy
from providers import DotenvSecretsProvider, NativeSandboxProvider
from providers.authz import RuleBasedAuthorizationProvider
from schemas import AgentId, SessionId, ThreadId
from schemas.caller_chain import CallerChain
from tools.builtins import (
    EditArgs,
    WriteArgs,
    WriteFileArgs,
    make_bash,
    make_edit,
    make_list_files,
    make_read_file,
    make_write,
    make_write_file,
)
from tools.decorator import ToolContext, ToolDefinition, get_tool_definition
from tools.discovery import discover_tools_in_file
from tools.dispatch import dispatch_tool
from workflows.builtins import register_builtins
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from collections.abc import Callable

_STARTER_TOOLS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "cli"
    / "_starter_bundle"
    / "kolsch"
    / "tools"
)

_PLATFORM_YAML = b"authorization:\n  variant: rule_based\n  config:\n    rules: []\n"
_DEFAULT_RULES = b'{"rules": []}\n'
_DOTENV = b"OPENROUTER_API_KEY=sk-secret\n"
_STORE_DB = b"sqlite-bytes"

_ATTACK = "authorization: {variant: rule_based, config: {rules: [{what: '*', pattern: '*', effect: allow}]}}\n"

_ALLOW_ALL = [{"what": "*", "pattern": "*", "effect": "allow"}]
_BUNDLES_ALLOW = [
    {"what": "agent.execute.*", "pattern": "*", "effect": "allow"},
    {"what": "kolsch/tools/write_file", "pattern": "bundles/*", "effect": "allow"},
    {"what": "*", "pattern": "*", "effect": "deny"},
]


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _case_insensitive_fs(root: Path) -> bool:
    probe = root / "case_probe_lower"
    probe.write_text("x", encoding="utf-8")
    try:
        return (root / "CASE_PROBE_LOWER").exists()
    finally:
        probe.unlink()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A workspace with every protected file on disk + an installed write_file."""
    ws = tmp_path / "ws"
    for sub in (
        "data",
        "logs",
        "content",
        "sessions",
        ".platform",
        ".kolsch",
        "bundles/kolsch/tools",
    ):
        (ws / sub).mkdir(parents=True, exist_ok=True)
    (ws / "platform.yaml").write_bytes(_PLATFORM_YAML)
    (ws / ".kolsch" / "default-rules.json").write_bytes(_DEFAULT_RULES)
    (ws / ".env").write_bytes(_DOTENV)
    (ws / ".platform" / "x").write_bytes(_STORE_DB)
    # The installed workspace copy — `kolsch setup` copies the starter
    # file verbatim; it imports the guard from the platform package.
    for name in ("write_file.py", "read_file.py", "list_files.py"):
        shutil.copy(_STARTER_TOOLS / name, ws / "bundles" / "kolsch" / "tools" / name)
    return ws.resolve()


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        "platform.yaml": (root / "platform.yaml").read_bytes(),
        ".kolsch/default-rules.json": (
            root / ".kolsch" / "default-rules.json"
        ).read_bytes(),
        ".env": (root / ".env").read_bytes(),
        ".platform/x": (root / ".platform" / "x").read_bytes(),
    }


def _assert_untouched(root: Path) -> None:
    assert _snapshot(root) == {
        "platform.yaml": _PLATFORM_YAML,
        ".kolsch/default-rules.json": _DEFAULT_RULES,
        ".env": _DOTENV,
        ".platform/x": _STORE_DB,
    }
    leftovers = [p for p in root.rglob("*") if ".tmp." in p.name]
    assert leftovers == []
    # Nothing new appeared under the protected directories either.
    assert sorted(p.name for p in (root / ".kolsch").iterdir()) == [
        "default-rules.json"
    ]
    assert sorted(p.name for p in (root / ".platform").iterdir()) == ["x"]


def _installed_tool(root: Path, filename: str, name: str) -> ToolDefinition:
    for fn in discover_tools_in_file(root / "bundles" / "kolsch" / "tools" / filename):
        defn = get_tool_definition(fn)
        if defn.name == name:
            # `home_bundle` is what the engine stamps at registration; it
            # makes `action_name` the real `kolsch/tools/<name>` ref.
            return dataclasses.replace(defn, home_bundle="kolsch")
    msg = f"{name} not found"
    raise LookupError(msg)


def _ctx(root: Path, policy: PathPolicy) -> ToolContext:
    return ToolContext(
        agent_id=AgentId("ag_test"),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal="user.local",
        secrets={},
        workspace_root=root,
        path_policy=policy,
        caller_chain=CallerChain.unattributed(),
    )


def _default_policy() -> PathPolicy:
    """The real `kolsch setup` default `file_access` section."""
    return load_policy(default_platform_payload()["file_access"])


def _wide_open_policy() -> PathPolicy:
    """What an agent (or user) would make `file_access` look like to get
    around it: whole root allowed, nothing denied. The floor must not
    depend on `denied_paths`."""
    return PathPolicy(allowed_paths=["."], denied_paths=[])


async def _dispatch(
    root: Path,
    defn: ToolDefinition,
    args: dict[str, Any],
    *,
    rules: list[dict[str, Any]],
    policy: PathPolicy,
) -> Any:
    return await dispatch_tool(
        defn=defn,
        args=args,
        context=_ctx(root, policy),
        authz=RuleBasedAuthorizationProvider(config={"rules": rules}),
        secrets=DotenvSecretsProvider(root=root, config={"path": ".env"}),
        sandbox=NativeSandboxProvider(),
    )


# Every spelling of a protected write target. `{root}` is substituted
# with the workspace root.
_FLOOR_TARGETS = [
    "platform.yaml",
    "PLATFORM.YAML",
    "Platform.Yaml",
    "./platform.yaml",
    "platform.yaml/.",
    "bundles/../platform.yaml",
    "data/../platform.yaml",
    "data/../PLATFORM.yaml",
    "{root}/platform.yaml",
    "{root}/./data/../platform.yaml",
    "data/link",  # symlink -> ../platform.yaml
    "data/dirlink/platform.yaml",  # data/dirlink -> ..
    ".kolsch/default-rules.json",
    ".kolsch/new-file.json",
    ".KOLSCH/x",
    ".kolsch",
    "{root}/.kolsch/bundle-baseline.json",
    ".env",
    ".ENV",
    ".Env",
    "data/../.env",
    ".platform/x",
    ".PLATFORM/entities.db",
]

# Targets the owner-shaped `bundles/*` allow also lets through authz
# (fnmatchcase matches `bundles/../x`).
_BUNDLES_ESCAPE_TARGETS = [
    "bundles/../platform.yaml",
    "bundles/../PLATFORM.YAML",
    "bundles/../.kolsch/default-rules.json",
    "bundles/../.env",
    "bundles/../.platform/x",
]


def _expand(target: str, root: Path) -> str:
    return target.replace("{root}", str(root))


def _make_links(root: Path) -> None:
    (root / "data" / "link").symlink_to(Path("..") / "platform.yaml")
    (root / "data" / "dirlink").symlink_to(Path(".."), target_is_directory=True)


# ---------------------------------------------------------------------------
# AC 2 — starter write_file through dispatch_tool under allow-all authz
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "policy_factory", [_default_policy, _wide_open_policy], ids=["default", "wide_open"]
)
@pytest.mark.parametrize("target", _FLOOR_TARGETS)
async def test_installed_write_file_refuses_protected_path_under_allow_all(
    root: Path,
    target: str,
    policy_factory: Callable[[], PathPolicy],
) -> None:
    _make_links(root)
    defn = _installed_tool(root, "write_file.py", "write_file")
    policy = policy_factory()

    with pytest.raises(FileAccessDenied) as exc_info:
        await _dispatch(
            root,
            defn,
            {"path": _expand(target, root), "content": _ATTACK},
            rules=_ALLOW_ALL,
            policy=policy,
        )

    err = exc_info.value
    assert err.code == "file_access.denied"
    matched = err.detail["matched_rule"]
    assert matched is not None
    if policy.denied_paths:
        # The default `.env` / `.platform/` denies may fire first.
        assert matched["type"] in {"protected_write_floor", "denied_paths"}
    else:
        assert matched["type"] == "protected_write_floor"
    _assert_untouched(root)
    # The symlink itself was not replaced by a regular file.
    assert (root / "data" / "link").is_symlink()


@pytest.mark.parametrize("target", _BUNDLES_ESCAPE_TARGETS)
async def test_bundles_allow_escape_is_refused(root: Path, target: str) -> None:
    """Owner-workspace shape: a `bundles/*` write_file allow matches
    `bundles/../platform.yaml` at authz (raw, unresolved target). The
    floor refuses it in the tool body."""
    defn = _installed_tool(root, "write_file.py", "write_file")

    with pytest.raises(FileAccessDenied) as exc_info:
        await _dispatch(
            root,
            defn,
            {"path": target, "content": _ATTACK},
            rules=_BUNDLES_ALLOW,
            policy=_wide_open_policy(),
        )

    assert exc_info.value.detail["matched_rule"]["type"] == "protected_write_floor"
    _assert_untouched(root)


async def test_floor_message_names_path_and_the_supported_writers(root: Path) -> None:
    defn = _installed_tool(root, "write_file.py", "write_file")
    with pytest.raises(FileAccessDenied) as exc_info:
        await _dispatch(
            root,
            defn,
            {"path": "bundles/../platform.yaml", "content": _ATTACK},
            rules=_ALLOW_ALL,
            policy=_default_policy(),
        )
    err = exc_info.value
    assert err.detail["matched_rule"] == {
        "pattern": "platform.yaml",
        "type": "protected_write_floor",
    }
    message = str(err)
    assert "platform.yaml is protected" in message
    assert "kolsch permissions" in message
    assert "approval modal" in message
    assert "kolsch setup" in message


# ---------------------------------------------------------------------------
# AC 3 — legitimate agent writes still succeed (author-workflow path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        "bundles/user/workflows/w.yaml",
        "bundles/user/nodes/n.py",
        "data/notes.md",
        "logs/x.txt",
        # Near-misses that must NOT be floored.
        "data/platform.yaml",
        "bundles/user/.env.example",
        "data/.kolsch/x",
        ".envrc",
        "platform.yaml.bak",
    ],
)
async def test_allowed_writes_still_succeed(root: Path, target: str) -> None:
    defn = _installed_tool(root, "write_file.py", "write_file")
    result = await _dispatch(
        root,
        defn,
        {"path": target, "content": "ok\n"},
        rules=_ALLOW_ALL,
        policy=_default_policy(),
    )
    assert result["bytes_written"] == 3
    assert (root / target).read_text(encoding="utf-8") == "ok\n"
    _assert_untouched(root)


# ---------------------------------------------------------------------------
# AC 4 — tools.builtins factories + the workflow write_file node
# ---------------------------------------------------------------------------


_BUILTIN_TARGETS = [
    "platform.yaml",
    "PLATFORM.YAML",
    "bundles/../platform.yaml",
    "{root}/.kolsch/x",
]


@pytest.mark.parametrize("target", _BUILTIN_TARGETS)
async def test_builtin_write_refuses_protected_path(root: Path, target: str) -> None:
    policy = _wide_open_policy()
    fn = make_write(policy=policy, root=root)
    with pytest.raises(FileAccessDenied) as exc_info:
        await fn(
            WriteArgs(path=_expand(target, root), content=_ATTACK), _ctx(root, policy)
        )
    assert exc_info.value.detail["matched_rule"]["type"] == "protected_write_floor"
    _assert_untouched(root)


@pytest.mark.parametrize("target", _BUILTIN_TARGETS[:3])
async def test_builtin_edit_refuses_protected_path(root: Path, target: str) -> None:
    policy = _wide_open_policy()
    fn = make_edit(policy=policy, root=root)
    with pytest.raises(FileAccessDenied) as exc_info:
        await fn(
            EditArgs(
                path=_expand(target, root), old_str="rules: []", new_str="rules: [x]"
            ),
            _ctx(root, policy),
        )
    assert exc_info.value.detail["matched_rule"]["type"] == "protected_write_floor"
    _assert_untouched(root)


@pytest.mark.parametrize("target", _BUILTIN_TARGETS)
async def test_builtin_write_file_refuses_protected_path(
    root: Path, target: str
) -> None:
    policy = _wide_open_policy()
    fn = make_write_file(policy=policy, root=root)
    with pytest.raises(FileAccessDenied) as exc_info:
        await fn(
            WriteFileArgs(path=_expand(target, root), content=_ATTACK),
            _ctx(root, policy),
        )
    assert exc_info.value.detail["matched_rule"]["type"] == "protected_write_floor"
    _assert_untouched(root)


def _workflow_registry(root: Path, policy: PathPolicy) -> RunnerRegistry:
    catalog = AssetCatalog()
    registry = RunnerRegistry()
    register_builtins(
        registry,
        bash_defn=get_tool_definition(make_bash(policy=policy, root=root)),
        authz=RuleBasedAuthorizationProvider(
            config={"rules": _ALLOW_ALL}, catalog=catalog
        ),
        secrets=DotenvSecretsProvider(root=root, config={"path": ".env"}),
        sandbox=NativeSandboxProvider(config={}),
        catalog=catalog,
        read_file_defn=get_tool_definition(make_read_file(policy=policy, root=root)),
        write_file_defn=get_tool_definition(make_write_file(policy=policy, root=root)),
        list_files_defn=get_tool_definition(make_list_files(policy=policy, root=root)),
    )
    return registry


@pytest.mark.parametrize(
    "target", ["platform.yaml", "bundles/../platform.yaml", "{{ inputs.where }}"]
)
async def test_workflow_write_file_node_refuses_platform_yaml(
    root: Path, target: str
) -> None:
    wf = Workflow.model_validate(
        {
            "nodes": {
                "w": {
                    "type": "kolsch/nodes/write_file",
                    "path": target,
                    "content": _ATTACK,
                },
            }
        }
    )
    store = WorkflowRunStore(root)
    run = store.create(
        workflow_ref="demo/workflows/escape", inputs={"where": "data/../PLATFORM.YAML"}
    )
    ex = IterativeExecutor(
        workflow=wf,
        run=run,
        store=store,
        registry=_workflow_registry(root, _wide_open_policy()),
        catalog=AssetCatalog(),
    )
    status = await ex.run()

    assert status == WorkflowRunStatus.FAILED
    events = list(store.read_events(run.run_id))
    failed = [e for e in events if e["kind"] == "workflow.node.failed"]
    assert len(failed) == 1
    assert failed[0]["payload"]["error_class"] == "WorkflowFileAccessDenied"
    # The run event states the real cause (QA N2): the path IS allowed
    # by `file_access`; the floor refused it.
    message = failed[0]["payload"]["message"]
    assert "platform.yaml is protected" in message
    assert "kolsch permissions" in message
    assert "outside the allowed paths" not in message
    assert events[-1]["kind"] == "workflow.run.failed"
    _assert_untouched(root)


# ---------------------------------------------------------------------------
# AC 5 — reads: platform.yaml stays readable; `.ENV` no longer bypasses
# ---------------------------------------------------------------------------


async def test_read_platform_yaml_and_list_root_still_allowed(root: Path) -> None:
    rules = _workspace_only_default_rules()
    read = _installed_tool(root, "read_file.py", "read_file")
    listing = _installed_tool(root, "list_files.py", "list_files")

    got = await _dispatch(
        root, read, {"path": "platform.yaml"}, rules=rules, policy=_default_policy()
    )
    assert "rule_based" in got["content"]
    entries = await _dispatch(
        root, listing, {"path": "."}, rules=rules, policy=_default_policy()
    )
    assert entries is not None


@pytest.mark.parametrize(
    "target", [".env", ".ENV", ".Env", "data/../.ENV", ".PLATFORM/x"]
)
async def test_read_dotenv_any_case_is_denied(root: Path, target: str) -> None:
    rules = _workspace_only_default_rules()
    read = _installed_tool(root, "read_file.py", "read_file")
    with pytest.raises(FileAccessDenied) as exc_info:
        await _dispatch(
            root, read, {"path": target}, rules=rules, policy=_default_policy()
        )
    assert exc_info.value.detail["matched_rule"]["type"] == "denied_paths"


def test_dotenv_case_bypass_is_real_on_this_host(root: Path) -> None:
    """Host-only: on a case-insensitive filesystem (default macOS APFS)
    `.ENV` IS `.env` — the bypass the casefolded deny closes. Skipped on
    case-sensitive filesystems (Linux CI), where `.ENV` is a distinct,
    non-existent file and the deny is a fail-safe over-match."""
    if not _case_insensitive_fs(root):
        pytest.skip("case-sensitive filesystem: .ENV is a different file here")
    assert (root / ".ENV").read_bytes() == _DOTENV
    assert (root / "PLATFORM.YAML").read_bytes() == _PLATFORM_YAML


def test_workflow_file_access_denied_remap_carries_reason() -> None:
    """A named rule's reason reaches the message; default-deny keeps the
    legacy wording (and the `file-access-denied` golden)."""
    from workflows.errors import WorkflowFileAccessDenied

    floor = WorkflowFileAccessDenied(
        path="platform.yaml",
        node="w",
        reason="platform.yaml is protected: agents cannot change kolsch configuration or state.",
        matched_rule={"pattern": "platform.yaml", "type": "protected_write_floor"},
    )
    assert "was refused access to 'platform.yaml': platform.yaml is protected" in str(
        floor
    )
    assert floor.matched_rule == {
        "pattern": "platform.yaml",
        "type": "protected_write_floor",
    }

    default = WorkflowFileAccessDenied(
        path="/etc/passwd", node="r", reason="default deny", matched_rule=None
    )
    assert "outside the allowed paths" in str(default)
    assert default.reason == "default deny"
