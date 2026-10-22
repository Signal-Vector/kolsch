"""bug-162 — an "always" approval must not overwrite on-disk rule edits.

The chat bootstraps a `RuleBasedAuthorizationProvider` from
`platform.yaml` once, then registers its approval modal with an
`on_rule_added` hook that persists the "always" rule. Before the fix
the hook wrote the provider's whole in-memory rule list (the startup
snapshot plus the new rule) over `authorization.config.rules`, so any
rule edit made on disk while the chat was open — a re-added
default-deny tail, an added `deny`, a removed `allow` — was silently
undone.

Each case bootstraps from a file, edits the file on disk, answers
"always" (a tuple return from the modal) and asserts on the disk
result. `_wire_like_chat` mirrors the chat's production wiring
(`src/cli/chat/app.py` `on_mount`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from cli.permissions import _write_back
from kolsch._yamlio import dump, load
from providers import AuthzEffect, Rule, RuleBasedAuthorizationProvider

if TYPE_CHECKING:
    from pathlib import Path

# Phase 1 (failing-first): cases (a)-(e) are red against origin/main's
# wiring. Phase 2 retargets `_wire_like_chat` at the new hook and drops
# this marker; the disk setup and assertions stay unchanged.
_RED_ON_MAIN = pytest.mark.xfail(
    strict=True,
    reason="bug-162: stale full-list write-back (fixed in Phase 2)",
)

_READ = {"what": "kolsch/tools/read_file", "pattern": "*", "effect": "allow"}
_WRITE = {"what": "kolsch/tools/write_file", "pattern": "*", "effect": "allow"}
_BASH_PROMPT = {"what": "kolsch/tools/bash", "pattern": "*", "effect": "prompt"}
_TAIL = {"what": "*", "pattern": "*", "effect": "deny"}
_RM_DENY = {"what": "kolsch/tools/bash", "pattern": "rm *", "effect": "deny"}

_OTHER_KEYS: dict[str, Any] = {
    "defaults": {"model": "mock/echo", "temperature": 0.2},
    "file_access": {"allowed_paths": ["."], "denied_paths": [".platform/**"]},
    "workspace_summary": "bug-162 fixture\nsecond line\n",
}


def _write_platform(yaml_path: Path, rules: list[dict[str, str]]) -> None:
    payload: dict[str, Any] = {
        "authorization": {"variant": "rule_based", "config": {"rules": rules}},
        **_OTHER_KEYS,
    }
    yaml_path.write_text(dump(payload), encoding="utf-8")


def _disk_payload(yaml_path: Path) -> dict[str, Any]:
    payload = load(yaml_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _disk_rules(yaml_path: Path) -> list[tuple[str, str, str]]:
    rules = _disk_payload(yaml_path)["authorization"]["config"]["rules"]
    return [(r["what"], r.get("pattern", "*"), r["effect"]) for r in rules]


def _key(entry: dict[str, str]) -> tuple[str, str, str]:
    return (entry["what"], entry.get("pattern", "*"), entry["effect"])


def _edit_disk_rules(yaml_path: Path, edit: Any) -> list[tuple[str, str, str]]:
    """Apply `edit(rules)` to the on-disk rule list (a hand edit / CLI run)."""
    payload = _disk_payload(yaml_path)
    rules = payload["authorization"]["config"]["rules"]
    edit(rules)
    yaml_path.write_text(dump(payload), encoding="utf-8")
    return _disk_rules(yaml_path)


def _bootstrap(yaml_path: Path) -> RuleBasedAuthorizationProvider:
    """Build the provider from disk, as `bootstrap_runtime` does at chat start."""
    config = _disk_payload(yaml_path)["authorization"]["config"]
    return RuleBasedAuthorizationProvider(config=config)


def _wire_like_chat(
    provider: RuleBasedAuthorizationProvider,
    yaml_path: Path,
    *,
    pattern: str = "*",
) -> list[dict[str, Any]]:
    """Register an "always allow" modal + the chat's persist hook.

    Returns the list the modal appends each call's kwargs to.
    """
    calls: list[dict[str, Any]] = []

    async def modal(**kwargs: Any) -> tuple[AuthzEffect, Rule]:
        calls.append(kwargs)
        return (
            AuthzEffect.ALLOW,
            Rule(what=kwargs["action"], pattern=pattern, effect="allow"),
        )

    provider.register_prompt_callback(
        modal,
        on_rule_added=lambda rules: _write_back(yaml_path, rules),
    )
    return calls


_LIST_FILES_ALWAYS = ("kolsch/tools/list_files", "*", "allow")


@_RED_ON_MAIN
async def test_a_disk_tail_added_after_bootstrap_survives_no_rule_always(
    tmp_path: Path,
) -> None:
    yaml_path = tmp_path / "platform.yaml"
    _write_platform(yaml_path, [_READ])  # tail-less, like the owner's workspace
    provider = _bootstrap(yaml_path)
    calls = _wire_like_chat(provider, yaml_path)
    edited = _edit_disk_rules(yaml_path, lambda rs: rs.append(dict(_TAIL)))

    result = await provider.allow("agent.ag_x", "kolsch/tools/list_files", target=".")

    assert result.allowed
    assert len(calls) == 1
    assert _disk_rules(yaml_path) == [*edited[:-1], _LIST_FILES_ALWAYS, edited[-1]], (
        "the disk tail was lost or the new rule is not immediately before it"
    )


@_RED_ON_MAIN
async def test_b_disk_added_deny_survives_no_rule_always(tmp_path: Path) -> None:
    yaml_path = tmp_path / "platform.yaml"
    _write_platform(yaml_path, [_READ, _WRITE])
    provider = _bootstrap(yaml_path)
    _wire_like_chat(provider, yaml_path)
    edited = _edit_disk_rules(yaml_path, lambda rs: rs.insert(0, dict(_RM_DENY)))

    await provider.allow("agent.ag_x", "kolsch/tools/list_files", target=".")

    assert _disk_rules(yaml_path) == [*edited, _LIST_FILES_ALWAYS], (
        "the deny added on disk after bootstrap was erased"
    )


@_RED_ON_MAIN
async def test_c_disk_removed_rule_stays_removed_no_rule_always(
    tmp_path: Path,
) -> None:
    yaml_path = tmp_path / "platform.yaml"
    _write_platform(yaml_path, [_READ, _WRITE])
    provider = _bootstrap(yaml_path)
    _wire_like_chat(provider, yaml_path)
    edited = _edit_disk_rules(
        yaml_path,
        lambda rs: rs.remove(next(r for r in rs if r["what"] == _WRITE["what"])),
    )

    await provider.allow("agent.ag_x", "kolsch/tools/list_files", target=".")

    assert _key(_WRITE) not in _disk_rules(yaml_path), (
        "the allow removed on disk after bootstrap was resurrected"
    )
    assert _disk_rules(yaml_path) == [*edited, _LIST_FILES_ALWAYS]


@_RED_ON_MAIN
async def test_d_prompt_rule_always_lands_before_disk_copy_of_prompt_rule(
    tmp_path: Path,
) -> None:
    yaml_path = tmp_path / "platform.yaml"
    _write_platform(yaml_path, [_READ, _WRITE, _BASH_PROMPT, _TAIL])
    provider = _bootstrap(yaml_path)
    calls = _wire_like_chat(provider, yaml_path, pattern="ls *")

    def edit(rs: list[dict[str, str]]) -> None:
        rs.insert(0, dict(_RM_DENY))  # shifts every index by one
        rs.remove(next(r for r in rs if r["what"] == _WRITE["what"]))

    edited = _edit_disk_rules(yaml_path, edit)
    prompt_at = edited.index(_key(_BASH_PROMPT))

    result = await provider.allow("agent.ag_x", "kolsch/tools/bash", target="ls -la")

    assert result.allowed
    assert calls[0]["rule_id"] is not None  # the prompt-rule path, not no-rule
    new_rule = ("kolsch/tools/bash", "ls *", "allow")
    assert _disk_rules(yaml_path) == [
        *edited[:prompt_at],
        new_rule,
        *edited[prompt_at:],
    ], "disk edits were lost or the rule is not right before the disk prompt rule"


_COMMENTED_PLATFORM = """\
# bug-162 header comment — must survive a rule write
authorization:
  variant: rule_based
  config:
    rules:
      - what: kolsch/tools/read_file  # inline comment on a rule
        pattern: '*'
        effect: allow
      - what: kolsch/tools/bash
        pattern: '*'
        effect: prompt
      - what: '*'
        pattern: '*'
        effect: deny
defaults:
  model: mock/echo
# trailing scaffold block
# hooks:
#   - event: tool.call.completed
"""

_COMMENT_LINES = (
    "# bug-162 header comment — must survive a rule write",
    "# inline comment on a rule",
    "# trailing scaffold block",
    "# hooks:",
    "#   - event: tool.call.completed",
)


@_RED_ON_MAIN
async def test_e_comment_lines_survive_a_rule_write(tmp_path: Path) -> None:
    yaml_path = tmp_path / "platform.yaml"
    yaml_path.write_text(_COMMENTED_PLATFORM, encoding="utf-8")
    provider = _bootstrap(yaml_path)
    _wire_like_chat(provider, yaml_path, pattern="ls *")

    await provider.allow("agent.ag_x", "kolsch/tools/bash", target="ls -la")

    text = yaml_path.read_text(encoding="utf-8")
    missing = [line for line in _COMMENT_LINES if line not in text]
    assert not missing, f"comment lines dropped by the rule write: {missing}"
    assert ("kolsch/tools/bash", "ls *", "allow") in _disk_rules(yaml_path)


async def test_f_other_top_level_keys_parse_identically(tmp_path: Path) -> None:
    yaml_path = tmp_path / "platform.yaml"
    _write_platform(yaml_path, [_READ, _BASH_PROMPT, _TAIL])
    provider = _bootstrap(yaml_path)
    _wire_like_chat(provider, yaml_path, pattern="ls *")
    before = _disk_payload(yaml_path)

    await provider.allow("agent.ag_x", "kolsch/tools/bash", target="ls -la")

    after = _disk_payload(yaml_path)
    assert set(after) == set(before)
    for key in _OTHER_KEYS:
        assert after[key] == before[key], f"top-level key {key!r} changed"
    assert after["authorization"]["variant"] == before["authorization"]["variant"]
