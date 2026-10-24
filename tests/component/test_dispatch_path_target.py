"""bug-163 — file-tool authz matches the canonical path through real dispatch.

`ToolsSubsystem.invoke_tool` → `dispatch_tool` with the starter
`read_file` / `write_file` / `list_files` tools, a real
`RuleBasedAuthorizationProvider` wrapped in a recording spy, and real
symlinks. Asserts decisions *and* filesystem side effects.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from errors import FileAccessDenied, ToolUnauthorized
from files import PathPolicy
from providers import DotenvSecretsProvider, NativeSandboxProvider
from providers.authz import AuthorizationResult, RuleBasedAuthorizationProvider
from schemas import AgentId, CallerChain, SessionId, ThreadId
from tools.decorator import ToolContext
from tools.subsystem import ToolsSubsystem

_STARTER_TOOLS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "cli"
    / "_starter_bundle"
    / "kolsch"
    / "tools"
)
_AGENT = AgentId("ag_bug163")
_TAIL: dict[str, str] = {"what": "*", "pattern": "*", "effect": "deny"}


class _SpyAuthz:
    """Delegates to a real provider and records every `allow` call."""

    def __init__(self, inner: RuleBasedAuthorizationProvider) -> None:
        self.inner = inner
        self.calls: list[dict[str, Any]] = []

    async def allow(
        self, principal: str, action: str, **kwargs: Any
    ) -> AuthorizationResult:
        self.calls.append({"action": action, **kwargs})
        return await self.inner.allow(principal, action, **kwargs)

    async def list_rules(self) -> list[Any]:
        return await self.inner.list_rules()


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = (tmp_path / "ws").resolve()
    (root / "bundles" / "u").mkdir(parents=True)
    (root / "data").mkdir()
    (root / ".env").write_text("", encoding="utf-8")
    (root / "bundles" / "u" / "w.yaml").write_text("w", encoding="utf-8")
    (root / "bundles" / "x").write_text("bx", encoding="utf-8")
    (root / "data" / "x.txt").write_text("dx", encoding="utf-8")
    (root / "data" / "dirlink").symlink_to("../bundles")
    (root / "bundles" / "blink").symlink_to("../data")
    return root


async def _subsystem(ws: Path, tool: str) -> tuple[ToolsSubsystem, _SpyAuthz]:
    spy = _SpyAuthz(
        RuleBasedAuthorizationProvider(
            config={
                "rules": [
                    {
                        "what": f"kolsch/tools/{tool}",
                        "pattern": "bundles/*",
                        "effect": "allow",
                    },
                    _TAIL,
                ]
            }
        )
    )
    subsystem = ToolsSubsystem(
        secrets=DotenvSecretsProvider(root=ws, config={"path": ".env"}),
        sandbox=NativeSandboxProvider(),
        authz=spy,
        path_policy=PathPolicy(allowed_paths=["."]),
        root=ws,
    )
    path = _STARTER_TOOLS / f"{tool}.py"
    await subsystem.register_for_agent(
        agent_id=_AGENT,
        declared_paths=[path],
        declared_owner_bundles={path: "kolsch"},
    )
    return subsystem, spy


def _ctx() -> ToolContext:
    return ToolContext(
        agent_id=_AGENT,
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal="user.local",
        secrets={},
        caller_chain=CallerChain.unattributed(),
    )


def _assert_spy_targets_are_display(spy: _SpyAuthz) -> None:
    assert spy.calls
    for call in spy.calls:
        pt = call["path_target"]
        assert pt is not None
        assert call["target"] == pt.display


def _expand(raw: str, ws: Path) -> str:
    return raw.replace("R/", f"{ws.as_posix()}/", 1) if raw.startswith("R/") else raw


# --- write_file -------------------------------------------------------------


async def test_write_traversal_out_of_bundles_is_unauthorized(ws: Path) -> None:
    subsystem, spy = await _subsystem(ws, "write_file")

    with pytest.raises(ToolUnauthorized):
        await subsystem.invoke_tool(
            "write_file", {"path": "bundles/../data/new.txt", "content": "p"}, _ctx()
        )

    assert not (ws / "data" / "new.txt").exists()
    assert spy.calls[0]["target"] == "data/new.txt"
    _assert_spy_targets_are_display(spy)


@pytest.mark.parametrize(
    ("raw", "written"),
    [
        ("bundles/u/w.yaml", "bundles/u/w.yaml"),
        ("./bundles/u/w.yaml", "bundles/u/w.yaml"),
        ("R/bundles/u/w.yaml", "bundles/u/w.yaml"),
        ("data/dirlink/new", "bundles/new"),
    ],
)
async def test_write_inside_bundles_succeeds_in_any_spelling(
    ws: Path, raw: str, written: str
) -> None:
    subsystem, spy = await _subsystem(ws, "write_file")

    await subsystem.invoke_tool(
        "write_file", {"path": _expand(raw, ws), "content": "ok"}, _ctx()
    )

    assert (ws / written).read_text(encoding="utf-8") == "ok"
    _assert_spy_targets_are_display(spy)


async def test_write_through_symlink_out_of_bundles_is_unauthorized(ws: Path) -> None:
    subsystem, spy = await _subsystem(ws, "write_file")

    with pytest.raises(ToolUnauthorized):
        await subsystem.invoke_tool(
            "write_file", {"path": "bundles/blink/new", "content": "p"}, _ctx()
        )

    assert not (ws / "data" / "new").exists()
    assert spy.calls[0]["target"] == "data/new"


async def test_write_nul_path_fails_closed_before_authz(ws: Path) -> None:
    subsystem, spy = await _subsystem(ws, "write_file")

    with pytest.raises(FileAccessDenied) as excinfo:
        await subsystem.invoke_tool(
            "write_file", {"path": "bundles/a\x00b", "content": "p"}, _ctx()
        )

    assert excinfo.value.code == "file_access.denied"
    assert excinfo.value.detail["path"] == "bundles/a\x00b"
    assert spy.calls == []
    assert sorted(p.name for p in (ws / "bundles").iterdir()) == ["blink", "u", "x"]


# --- read_file --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "allowed"),
    [
        ("bundles/../data/x.txt", False),
        ("bundles/blink/x.txt", False),
        ("bundles/u/w.yaml", True),
        ("./bundles/u/w.yaml", True),
        ("R/bundles/u/w.yaml", True),
        ("data/dirlink/x", True),
    ],
)
async def test_read_matrix(ws: Path, raw: str, *, allowed: bool) -> None:
    subsystem, spy = await _subsystem(ws, "read_file")
    call = subsystem.invoke_tool("read_file", {"path": _expand(raw, ws)}, _ctx())

    if allowed:
        result = await call
        assert result.result is not None
    else:
        with pytest.raises(ToolUnauthorized):
            await call
    _assert_spy_targets_are_display(spy)


# --- list_files -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "allowed"),
    [
        ("bundles/../data", False),
        ("bundles/blink", False),
        ("bundles/u", True),
        ("./bundles/u/", True),
        ("R/bundles/u", True),
        ("data/dirlink/u", True),
    ],
)
async def test_list_matrix(ws: Path, raw: str, *, allowed: bool) -> None:
    subsystem, spy = await _subsystem(ws, "list_files")
    call = subsystem.invoke_tool("list_files", {"path": _expand(raw, ws)}, _ctx())

    if allowed:
        await call
    else:
        with pytest.raises(ToolUnauthorized):
            await call
    _assert_spy_targets_are_display(spy)
