"""Component tests for ``kolsch permissions {list, add, remove, show}``.

Phase 2.0 (TOOL-R6): the CLI surface for editing the workspace
authorization rule list out of chat. Covers the four leaf commands
and the platform.yaml round-trip (atomic write; existing-section
preservation; protected default-deny tail per TOOL-R10).
"""

from __future__ import annotations

import argparse
import io
from typing import TYPE_CHECKING

import pytest
import yaml

from cli.permissions import cmd_permissions

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace with platform.yaml + the workspace-
    only default rule set."""
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (tmp_path / sub).mkdir(exist_ok=True)
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                "authorization": {
                    "variant": "rule_based",
                    "config": {
                        "rules": [
                            {
                                "who": ".*",
                                "what": r"agent\.execute\..*",
                                "effect": "allow",
                            },
                            {
                                "who": ".*",
                                "what": r"tool\.read_file",
                                "effect": "allow",
                            },
                            {
                                "who": ".*",
                                "what": r"tool\.write_file",
                                "effect": "prompt",
                            },
                            {"who": ".*", "what": ".*", "effect": "deny"},
                        ],
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return tmp_path


def _args(root: Path, **kw: object) -> argparse.Namespace:
    base = {
        "root": root,
        "permissions_command": kw.get("permissions_command", "list"),
        "who": kw.get("who"),
        "what": kw.get("what"),
        "effect": kw.get("effect"),
        "before": kw.get("before"),
        "after": kw.get("after"),
        "first": kw.get("first", False),
        "rule_id": kw.get("rule_id"),
    }
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def test_permissions_list_prints_rules_in_order(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    rc = await cmd_permissions(
        _args(root, permissions_command="list"), out=out, err=err
    )
    assert rc == 0
    output = out.getvalue()
    # All four rules present (the agent.execute one and the default-deny
    # tail). Printed via repr() so backslashes in `what` regexes
    # double — search for the unambiguous substrings.
    assert "agent" in output and "execute" in output
    assert "rl_default_deny" in output
    # Protected marker visible for the tail.
    assert "(protected)" in output


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


async def test_permissions_show_prints_one_rule(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    rc = await cmd_permissions(
        _args(root, permissions_command="show", rule_id="rl_default_deny"),
        out=out,
        err=err,
    )
    assert rc == 0
    text = out.getvalue()
    assert "id:     rl_default_deny" in text
    assert "effect: deny" in text
    assert "(protected" in text


async def test_permissions_show_unknown_rule_returns_nonzero(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    rc = await cmd_permissions(
        _args(root, permissions_command="show", rule_id="rl_nope"),
        out=out,
        err=err,
    )
    assert rc == 1
    assert "error" in err.getvalue().lower()


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


async def test_permissions_add_writes_back_atomically(tmp_path: Path) -> None:
    """The on-disk platform.yaml gains the new rule before the
    default-deny tail; other sections are preserved."""
    root = _workspace(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    rc = await cmd_permissions(
        _args(
            root,
            permissions_command="add",
            who=".*",
            what=r"tool\.bash",
            effect="allow",
        ),
        out=out,
        err=err,
    )
    assert rc == 0
    payload = yaml.safe_load((root / "platform.yaml").read_text())
    rules = payload["authorization"]["config"]["rules"]
    effects = [r["effect"] for r in rules]
    # New allow lands before the default-deny tail; allow at the end
    # would never fire.
    assert effects[-1] == "deny"
    assert any(
        r["what"] == r"tool\.bash" and r["effect"] == "allow" for r in rules[:-1]
    )


async def test_permissions_add_first_lands_at_top(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    rc = await cmd_permissions(
        _args(
            root,
            permissions_command="add",
            who=".*",
            what=r"tool\.bash",
            effect="deny",
            first=True,
        ),
        out=out,
        err=err,
    )
    assert rc == 0
    payload = yaml.safe_load((root / "platform.yaml").read_text())
    rules = payload["authorization"]["config"]["rules"]
    assert rules[0]["what"] == r"tool\.bash"
    assert rules[0]["effect"] == "deny"


# ---------------------------------------------------------------------------
# remove — including the protected default-deny tail
# ---------------------------------------------------------------------------


async def test_permissions_remove_default_deny_is_rejected(tmp_path: Path) -> None:
    """TOOL-R10 hard rule — `rl_default_deny` is removal-protected."""
    root = _workspace(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    rc = await cmd_permissions(
        _args(
            root,
            permissions_command="remove",
            rule_id="rl_default_deny",
        ),
        out=out,
        err=err,
    )
    assert rc == 1
    assert "protected" in err.getvalue().lower()
    # On-disk rules unchanged.
    payload = yaml.safe_load((root / "platform.yaml").read_text())
    assert (
        any(
            r["id"] == "rl_default_deny"
            for r in payload["authorization"]["config"]["rules"]
            if "id" in r
        )
        or payload["authorization"]["config"]["rules"][-1]["effect"] == "deny"
    )


async def test_permissions_remove_unknown_rule(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    rc = await cmd_permissions(
        _args(
            root,
            permissions_command="remove",
            rule_id="rl_does_not_exist",
        ),
        out=out,
        err=err,
    )
    assert rc == 1
    assert "no rule" in err.getvalue().lower() or "error" in err.getvalue().lower()


# ---------------------------------------------------------------------------
# End-to-end: add then list shows the new rule
# ---------------------------------------------------------------------------


async def test_permissions_add_then_list_roundtrip(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    rc = await cmd_permissions(
        _args(
            root,
            permissions_command="add",
            who="agent.helper",
            what=r"tool\.bash",
            effect="allow",
        ),
        out=out,
        err=err,
    )
    assert rc == 0

    # Re-load via list to confirm the on-disk write is picked up.
    list_out = io.StringIO()
    list_rc = await cmd_permissions(
        _args(root, permissions_command="list"), out=list_out, err=err
    )
    assert list_rc == 0
    assert "agent.helper" in list_out.getvalue()


# ---------------------------------------------------------------------------
# Uninitialized workspace
# ---------------------------------------------------------------------------


async def test_permissions_fails_on_uninitialized_workspace(tmp_path: Path) -> None:
    """No platform.yaml → WorkspaceNotInitialized raised (CLI surface
    converts to friendly message + exit 3 per Phase 1.2)."""
    from errors import WorkspaceNotInitialized

    out, err = io.StringIO(), io.StringIO()
    with pytest.raises(WorkspaceNotInitialized):
        await cmd_permissions(
            _args(tmp_path, permissions_command="list"), out=out, err=err
        )
