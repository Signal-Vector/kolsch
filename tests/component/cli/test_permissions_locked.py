"""feat-160 — `kolsch permissions` and `/perm` with locked platform rules.

`add --locked` appends to `authorization.config.locked_rules` and leaves
`rules` untouched; flag misuse is refused without writing; `list` shows
a locked section first only when locked rules exist; `show` / `remove`
work on `rl_locked_` ids and `remove` persists; `/perm` round-trips
never touch `locked_rules`.
"""

from __future__ import annotations

import argparse
import io
from typing import TYPE_CHECKING, Any, cast

import pytest
import yaml

from cli._slash import ChatSessionState, SlashContext, dispatch_slash
from cli.permissions import cmd_permissions
from platform_runtime import bootstrap_runtime
from providers import derive_locked_rule_id

if TYPE_CHECKING:
    from pathlib import Path

_BASH = "kolsch/tools/bash"
_READ = "kolsch/tools/read_file"
_RULES: list[dict[str, Any]] = [
    {"what": "agent.execute.*", "pattern": "*", "effect": "allow"},
    {"what": _READ, "pattern": "*", "effect": "allow"},
    {"what": "*", "pattern": "*", "effect": "deny"},
]
_LOCKED_HEADER = (
    "locked (evaluated before workflow/agent/bundle layers; cannot be overridden):"
)


def _workspace(root: Path, *, locked: list[dict[str, Any]] | None = None) -> Path:
    from assets.platform_config import default_platform_payload

    for sub in ("sessions", "content", "logs", "data", ".platform"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    tools = root / "bundles" / "kolsch" / "tools"
    tools.mkdir(parents=True)
    (root / "bundles" / "kolsch" / "bundle.yaml").write_text(
        yaml.safe_dump(
            {"name": "kolsch", "version": "0.1.0", "description": "fixture"}
        ),
        encoding="utf-8",
    )
    for name in ("bash", "read_file"):
        (tools / f"{name}.py").write_text(f"# stub for {name}\n", encoding="utf-8")
    payload = default_platform_payload()
    config: dict[str, Any] = {"rules": list(_RULES)}
    if locked is not None:
        config["locked_rules"] = locked
    payload["authorization"] = {"variant": "rule_based", "config": config}
    payload["defaults"] = {}
    (root / "platform.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    return root


def _args(root: Path, op: str, **kw: Any) -> argparse.Namespace:
    return argparse.Namespace(
        root=root,
        permissions_command=op,
        what=kw.get("what"),
        pattern=kw.get("pattern", "*"),
        from_=kw.get("from_"),
        effect=kw.get("effect"),
        before=kw.get("before"),
        after=kw.get("after"),
        first=kw.get("first", False),
        locked=kw.get("locked", False),
        rule_id=kw.get("rule_id"),
    )


async def _run(root: Path, op: str, **kw: Any) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    rc = await cmd_permissions(_args(root, op, **kw), out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


def _config(root: Path) -> dict[str, Any]:
    payload = yaml.safe_load((root / "platform.yaml").read_text(encoding="utf-8"))
    return cast("dict[str, Any]", payload["authorization"]["config"])


_LOCKED_BASH_ID = derive_locked_rule_id(_BASH, "*", "deny")


# ---------------------------------------------------------------------------
# add --locked
# ---------------------------------------------------------------------------


async def test_add_locked_appends_to_locked_rules_only(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    before = _config(root)["rules"]
    rc, out, err = await _run(root, "add", what=_BASH, effect="deny", locked=True)
    assert rc == 0, err
    assert f"added locked rule {_LOCKED_BASH_ID}" in out
    cfg = _config(root)
    assert cfg["rules"] == before
    assert cfg["locked_rules"] == [{"what": _BASH, "pattern": "*", "effect": "deny"}]
    # A second lock appends after the first.
    rc, _, err = await _run(
        root, "add", what=_READ, pattern="/etc/*", effect="deny", locked=True
    )
    assert rc == 0, err
    assert [r["what"] for r in _config(root)["locked_rules"]] == [_BASH, _READ]
    assert _config(root)["rules"] == before


@pytest.mark.parametrize(
    "kw",
    [
        {"effect": "allow"},
        {"effect": "prompt"},
        {"effect": "deny", "from_": "kolsch/agents/x"},
        {"effect": "deny", "first": True},
        {"effect": "deny", "before": "rl_default_deny"},
        {"effect": "deny", "after": "rl_default_deny"},
        # Validation from the provider (B1: 2-segment ref), surfaced cleanly.
        {"effect": "deny", "what": "kolsch/bash"},
        # Duplicate of the existing lock.
        {"effect": "deny", "what": _BASH},
    ],
)
async def test_add_locked_misuse_refused_without_writing(
    tmp_path: Path, kw: dict[str, Any]
) -> None:
    root = _workspace(
        tmp_path, locked=[{"what": _BASH, "pattern": "*", "effect": "deny"}]
    )
    original = (root / "platform.yaml").read_bytes()
    kw = {"what": _READ, **kw}
    rc, _, err = await _run(root, "add", locked=True, **kw)
    assert rc == 1
    assert err.startswith("error: ")
    assert (root / "platform.yaml").read_bytes() == original


async def test_add_without_locked_preserves_locked_rules(tmp_path: Path) -> None:
    locked = [{"what": _BASH, "pattern": "*", "effect": "deny"}]
    root = _workspace(tmp_path, locked=locked)
    rc, _, err = await _run(root, "add", what=_BASH, pattern="ls *", effect="allow")
    assert rc == 0, err
    cfg = _config(root)
    assert cfg["locked_rules"] == locked
    assert any(r.get("pattern") == "ls *" for r in cfg["rules"])


# ---------------------------------------------------------------------------
# list / show / remove
# ---------------------------------------------------------------------------


async def test_list_without_locked_rules_has_no_locked_section(tmp_path: Path) -> None:
    root = _workspace(tmp_path / "a")
    rc, out, _ = await _run(root, "list")
    assert rc == 0
    assert "locked" not in out
    # Same output as a workspace whose `locked_rules` is empty.
    other = _workspace(tmp_path / "other", locked=[])
    assert (await _run(other, "list"))[1] == out


async def test_list_shows_locked_section_first(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path / "a", locked=[{"what": _BASH, "pattern": "*", "effect": "deny"}]
    )
    plain = _workspace(tmp_path / "plain")
    rc, out, _ = await _run(root, "list")
    assert rc == 0
    lines = out.splitlines()
    assert lines[0] == _LOCKED_HEADER
    assert lines[1].startswith(_LOCKED_BASH_ID)
    assert lines[1].endswith("(locked)")
    assert lines[3] == "rules:"
    # The platform rows that follow are exactly the no-lock listing.
    assert "\n".join(lines[4:]) + "\n" == (await _run(plain, "list"))[1]


async def test_show_marks_locked_rule(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path, locked=[{"what": _BASH, "pattern": "*", "effect": "deny"}]
    )
    rc, out, _ = await _run(root, "show", rule_id=_LOCKED_BASH_ID)
    assert rc == 0
    assert f"id:     {_LOCKED_BASH_ID}" in out
    assert "(locked — evaluated before every layer; cannot be overridden)" in out


async def test_remove_locked_rule_persists(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        locked=[
            {"what": _BASH, "pattern": "*", "effect": "deny"},
            {"what": _READ, "pattern": "/etc/*", "effect": "deny"},
        ],
    )
    before = _config(root)["rules"]
    rc, out, err = await _run(root, "remove", rule_id=_LOCKED_BASH_ID)
    assert rc == 0, err
    assert f"removed rule {_LOCKED_BASH_ID}" in out
    cfg = _config(root)
    assert cfg["locked_rules"] == [
        {"what": _READ, "pattern": "/etc/*", "effect": "deny"}
    ]
    assert cfg["rules"] == before
    # Removing the last lock drops the key.
    read_id = derive_locked_rule_id(_READ, "/etc/*", "deny")
    rc, _, err = await _run(root, "remove", rule_id=read_id)
    assert rc == 0, err
    assert "locked_rules" not in _config(root)
    # And a fresh load really has none (persisted, not just printed).
    rc, out, _ = await _run(root, "list")
    assert "locked" not in out


async def test_remove_platform_rule_preserves_locked_rules(tmp_path: Path) -> None:
    locked = [{"what": _BASH, "pattern": "*", "effect": "deny"}]
    root = _workspace(tmp_path, locked=locked)
    rc, _, err = await _run(root, "add", what=_BASH, pattern="ls *", effect="allow")
    assert rc == 0, err
    rc, out, _ = await _run(root, "list")
    rule_id = next(line.split()[0] for line in out.splitlines() if "'ls *'" in line)
    rc, _, err = await _run(root, "remove", rule_id=rule_id)
    assert rc == 0, err
    assert _config(root)["locked_rules"] == locked


# ---------------------------------------------------------------------------
# /perm — list section + round-trips leave locked_rules alone
# ---------------------------------------------------------------------------


def _slash_ctx(runtime: Any, stdout: io.StringIO) -> SlashContext:
    class _StubState:
        def __init__(self, runtime: Any) -> None:
            self.runtime = runtime

    return SlashContext(
        stdout=stdout, state=cast("ChatSessionState", _StubState(runtime))
    )


async def test_perm_round_trip_preserves_locked_rules(tmp_path: Path) -> None:
    locked = [{"what": _BASH, "pattern": "*", "effect": "deny"}]
    root = _workspace(tmp_path, locked=locked)
    runtime = await bootstrap_runtime(root=root)
    stdout = io.StringIO()
    ctx = _slash_ctx(runtime, stdout)
    await dispatch_slash('/perm allow kolsch/tools/bash "find *"', ctx)
    assert _config(root)["locked_rules"] == locked
    await dispatch_slash('/perm remove kolsch/tools/bash "find *"', ctx)
    cfg = _config(root)
    assert cfg["locked_rules"] == locked
    assert not any(r.get("pattern") == "find *" for r in cfg["rules"])
    assert not any(r.get("what") == _BASH for r in cfg["rules"])


async def test_perm_list_shows_locked_section_only_when_present(
    tmp_path: Path,
) -> None:
    locked_root = _workspace(
        tmp_path / "locked", locked=[{"what": _BASH, "pattern": "*", "effect": "deny"}]
    )
    plain_root = _workspace(tmp_path / "plain")
    outputs = []
    for root in (locked_root, plain_root):
        runtime = await bootstrap_runtime(root=root)
        stdout = io.StringIO()
        await dispatch_slash("/perm list", _slash_ctx(runtime, stdout))
        outputs.append(stdout.getvalue())
    locked_out, plain_out = outputs
    assert locked_out.splitlines()[0] == _LOCKED_HEADER
    assert _LOCKED_BASH_ID in locked_out
    assert "locked" not in plain_out
    assert locked_out.endswith(plain_out)


# ---------------------------------------------------------------------------
# mutate_platform_rules locked ops — structural, lock-guarded writes
# ---------------------------------------------------------------------------


_COMMENTED_PLATFORM = """\
# operator notes at the top
authorization:
  variant: rule_based
  config:
    rules:
      # let agents run
      - {what: agent.execute.*, pattern: '*', effect: allow}
      - {what: '*', pattern: '*', effect: deny}  # tail
storage:
  backend: sqlite
"""


def test_locked_insert_then_remove_is_structural(tmp_path: Path) -> None:
    from cli.permissions import (
        InsertLockedRule,
        RemoveLockedRule,
        mutate_platform_rules,
    )
    from providers import Rule

    (tmp_path / ".platform").mkdir()
    path = tmp_path / "platform.yaml"
    path.write_text(_COMMENTED_PLATFORM, encoding="utf-8")
    added = mutate_platform_rules(
        path, InsertLockedRule(Rule(what=_BASH, effect="deny")), catalog=None
    )
    assert added.rule.id == _LOCKED_BASH_ID
    assert added.position == 0
    text = path.read_text(encoding="utf-8")
    # Comments and the rules list survive verbatim; only the key is new.
    for line in _COMMENTED_PLATFORM.splitlines():
        assert line in text
    assert _config(tmp_path)["locked_rules"] == [
        {"what": _BASH, "pattern": "*", "effect": "deny"}
    ]
    removed = mutate_platform_rules(
        path, RemoveLockedRule(rule_id=_LOCKED_BASH_ID), catalog=None
    )
    assert removed.rule.id == _LOCKED_BASH_ID
    assert path.read_text(encoding="utf-8") == _COMMENTED_PLATFORM


def test_locked_ops_refuse_invalid_disk_list_and_unknown_ids(tmp_path: Path) -> None:
    from cli.permissions import (
        InsertLockedRule,
        RemoveLockedRule,
        mutate_platform_rules,
    )
    from errors import AuthzRuleNotFound, AuthzRulesPersistFailed
    from providers import Rule

    (tmp_path / ".platform").mkdir()
    path = tmp_path / "platform.yaml"
    path.write_text(
        _COMMENTED_PLATFORM.replace(
            "storage:",
            "    locked_rules:\n      - {what: kolsch/bash, effect: deny}\nstorage:",
        ),
        encoding="utf-8",
    )
    original = path.read_bytes()
    with pytest.raises(AuthzRulesPersistFailed):
        mutate_platform_rules(
            path, InsertLockedRule(Rule(what=_READ, effect="deny")), catalog=None
        )
    assert path.read_bytes() == original
    path.write_text(_COMMENTED_PLATFORM, encoding="utf-8")
    with pytest.raises(AuthzRuleNotFound):
        mutate_platform_rules(
            path, RemoveLockedRule(rule_id=_LOCKED_BASH_ID), catalog=None
        )
    assert path.read_text(encoding="utf-8") == _COMMENTED_PLATFORM


async def test_stale_chat_and_locked_add_clobber_neither_list(tmp_path: Path) -> None:
    """A chat holding a stale `rules` list and a CLI locked add interleave safely.

    The chat loads first; the CLI then adds a lock on disk; the chat's
    `/perm` writes (computed on the disk list, not its stale copy) keep
    the lock, and a later locked add keeps the chat's rule.
    """
    root = _workspace(tmp_path)
    runtime = await bootstrap_runtime(root=root)  # the chat's view, now stale
    rc, _, err = await _run(root, "add", what=_BASH, effect="deny", locked=True)
    assert rc == 0, err
    stdout = io.StringIO()
    ctx = _slash_ctx(runtime, stdout)
    await dispatch_slash('/perm allow kolsch/tools/read_file "/etc/*"', ctx)
    cfg = _config(root)
    assert cfg["locked_rules"] == [{"what": _BASH, "pattern": "*", "effect": "deny"}]
    assert any(r.get("pattern") == "/etc/*" for r in cfg["rules"])
    rc, _, err = await _run(
        root, "add", what=_READ, pattern="/etc/shadow", effect="deny", locked=True
    )
    assert rc == 0, err
    cfg = _config(root)
    assert [r["what"] for r in cfg["locked_rules"]] == [_BASH, _READ]
    assert any(r.get("pattern") == "/etc/*" for r in cfg["rules"])
    await dispatch_slash('/perm remove kolsch/tools/read_file "/etc/*"', ctx)
    cfg = _config(root)
    assert len(cfg["locked_rules"]) == 2
    assert not any(r.get("pattern") == "/etc/*" for r in cfg["rules"])
