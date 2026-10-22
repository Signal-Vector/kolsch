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
(`src/cli/chat/app.py` `on_mount`: `RulePersistHook`). On origin/main
the same cases (a)-(e), wired to `_write_back`, failed (Phase 1 notes).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from assets.platform_config import platform_yaml_lock
from cli.chat.approval import RulePersistHook
from kolsch._yamlio import dump, load
from providers import AuthzEffect, Rule, RuleBasedAuthorizationProvider

if TYPE_CHECKING:
    from pathlib import Path

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
    notices: list[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Register an "always allow" modal + the chat's persist hook.

    Returns the list the modal appends each call's kwargs to; chat
    notices land in `notices` as `(kind, text)`.
    """
    calls: list[dict[str, Any]] = []

    async def modal(**kwargs: Any) -> tuple[AuthzEffect, Rule]:
        calls.append(kwargs)
        return (
            AuthzEffect.ALLOW,
            Rule(what=kwargs["action"], pattern=pattern, effect="allow"),
        )

    sink = notices if notices is not None else []

    def notify(text: str, *, kind: str = "info") -> None:
        sink.append((kind, text))

    provider.register_prompt_callback(
        modal,
        on_rule_added=RulePersistHook(
            yaml_path=yaml_path,
            authz=lambda: provider,
            catalog=lambda: None,
            notify=notify,
        ),
    )
    return calls


_LIST_FILES_ALWAYS = ("kolsch/tools/list_files", "*", "allow")


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


# ---------------------------------------------------------------------------
# D1 — in-memory rules are not rebuilt from disk; drift notice
# ---------------------------------------------------------------------------


async def test_i_in_memory_rules_keep_snapshot_and_drift_is_noticed_once(
    tmp_path: Path,
) -> None:
    yaml_path = tmp_path / "platform.yaml"
    _write_platform(yaml_path, [_READ, _BASH_PROMPT, _TAIL])
    provider = _bootstrap(yaml_path)
    notices: list[tuple[str, str]] = []
    _wire_like_chat(provider, yaml_path, pattern="ls *", notices=notices)
    loaded = [_key_of(r) for r in provider.rules_snapshot()]
    _edit_disk_rules(yaml_path, lambda rs: rs.insert(0, dict(_RM_DENY)))

    await provider.allow("agent.ag_x", "kolsch/tools/bash", target="ls -la")

    new_rule = ("kolsch/tools/bash", "ls *", "allow")
    # D1: memory = loaded + the new rule; the disk-only deny is NOT live.
    assert [_key_of(r) for r in provider.rules_snapshot()] == [
        loaded[0],
        new_rule,
        *loaded[1:],
    ]
    drift = [text for kind, text in notices if "changed on disk" in text]
    assert len(drift) == 1 and "/reload" in drift[0]
    assert all(kind == "warning" for kind, _ in notices)

    # Same unapplied edit, another "always" (a target `ls *` doesn't
    # cover, so the prompt fires again): no second notice.
    await provider.allow("agent.ag_x", "kolsch/tools/bash", target="cat a")
    assert len([t for _, t in notices if "changed on disk" in t]) == 1

    # A new disk edit is a new drift → one more notice.
    _edit_disk_rules(yaml_path, lambda rs: rs.insert(0, dict(_WRITE)))
    await provider.allow("agent.ag_x", "kolsch/tools/bash", target="cat b")
    assert len([t for _, t in notices if "changed on disk" in t]) == 2


async def test_i_no_drift_notice_when_disk_matches_memory(tmp_path: Path) -> None:
    yaml_path = tmp_path / "platform.yaml"
    _write_platform(yaml_path, [_READ, _BASH_PROMPT, _TAIL])
    provider = _bootstrap(yaml_path)
    notices: list[tuple[str, str]] = []
    _wire_like_chat(provider, yaml_path, pattern="ls *", notices=notices)

    await provider.allow("agent.ag_x", "kolsch/tools/bash", target="ls -la")

    assert notices == []
    assert ("kolsch/tools/bash", "ls *", "allow") in _disk_rules(yaml_path)


def _key_of(rule: Rule) -> tuple[str, str, str]:
    return (rule.what, rule.pattern, rule.effect)


# ---------------------------------------------------------------------------
# (h) failure modes through the chat hook
# ---------------------------------------------------------------------------


def _break_missing(yaml_path: Path) -> None:
    yaml_path.unlink()


def _break_unparseable(yaml_path: Path) -> None:
    yaml_path.write_text("authorization: [unclosed\n", encoding="utf-8")


def _break_rules_not_list(yaml_path: Path) -> None:
    yaml_path.write_text(
        "authorization:\n  config:\n    rules: nope\n", encoding="utf-8"
    )


def _break_invalid_rules(yaml_path: Path) -> None:
    _edit_disk_rules(
        yaml_path,
        lambda rs: rs.insert(
            0, {"what": "kolsch/tools/bash", "pattern": "^rm$", "effect": "deny"}
        ),
    )


@pytest.mark.parametrize(
    ("breaker", "reason"),
    [
        (_break_missing, "missing"),
        (_break_unparseable, "unparseable"),
        (_break_rules_not_list, "unparseable"),
        (_break_invalid_rules, "invalid_rules"),
    ],
)
async def test_h_persist_failure_keeps_rule_live_and_notices(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    breaker: Any,
    reason: str,
) -> None:
    yaml_path = tmp_path / "platform.yaml"
    _write_platform(yaml_path, [_READ, _BASH_PROMPT, _TAIL])
    provider = _bootstrap(yaml_path)
    notices: list[tuple[str, str]] = []
    calls = _wire_like_chat(provider, yaml_path, pattern="ls *", notices=notices)
    breaker(yaml_path)
    before = yaml_path.read_bytes() if yaml_path.exists() else None

    with caplog.at_level("WARNING"):
        first = await provider.allow("agent.ag_x", "kolsch/tools/bash", target="ls a")
    second = await provider.allow("agent.ag_x", "kolsch/tools/bash", target="ls b")

    assert first.allowed and second.allowed
    assert len(calls) == 1, "the in-memory rule must answer the second call"
    if before is None:
        assert not yaml_path.exists(), "a missing platform.yaml must not be created"
    else:
        assert yaml_path.read_bytes() == before
    assert len(notices) == 1
    kind, text = notices[0]
    assert kind == "warning"
    assert "active for this chat only" in text
    assert str(yaml_path) in text
    assert reason in text
    assert "kolsch permissions add --what kolsch/tools/bash --pattern 'ls *'" in text
    assert any("not saved" in r.getMessage() for r in caplog.records)


async def test_h_lock_held_and_write_failure_through_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cli.permissions as permissions_mod

    yaml_path = tmp_path / "platform.yaml"
    _write_platform(yaml_path, [_READ, _BASH_PROMPT, _TAIL])
    provider = _bootstrap(yaml_path)
    notices: list[tuple[str, str]] = []
    _wire_like_chat(provider, yaml_path, pattern="ls *", notices=notices)
    before = yaml_path.read_bytes()

    # Lock held (in this process — a separate fd still conflicts).
    with platform_yaml_lock(tmp_path):
        assert (
            await provider.allow("agent.ag_x", "kolsch/tools/bash", target="ls a")
        ).allowed
    assert yaml_path.read_bytes() == before
    assert "lock_timeout" in notices[-1][1]

    # Unwritable destination.
    def boom(*_args: object) -> None:
        msg = "read-only file system"
        raise OSError(msg)

    monkeypatch.setattr(permissions_mod.os, "replace", boom)
    provider2 = _bootstrap(yaml_path)
    _wire_like_chat(provider2, yaml_path, pattern="ls *", notices=notices)
    assert (
        await provider2.allow("agent.ag_x", "kolsch/tools/bash", target="ls a")
    ).allowed
    assert yaml_path.read_bytes() == before
    assert "write_failed" in notices[-1][1]
    assert len(notices) == 2


async def test_hook_writes_off_the_event_loop_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    import cli.permissions as permissions_mod

    yaml_path = tmp_path / "platform.yaml"
    _write_platform(yaml_path, [_READ, _BASH_PROMPT, _TAIL])
    provider = _bootstrap(yaml_path)
    _wire_like_chat(provider, yaml_path, pattern="ls *")
    real = permissions_mod.persist_rule_request
    threads: list[int] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(permissions_mod, "persist_rule_request", spy)
    await provider.allow("agent.ag_x", "kolsch/tools/bash", target="ls")

    assert threads and threads[0] != threading.get_ident()
    assert ("kolsch/tools/bash", "ls *", "allow") in _disk_rules(yaml_path)


async def test_layer_prompt_match_persists_before_the_tail_not_an_anchor(
    tmp_path: Path,
) -> None:
    """A prompt rule from a bundle/agent/workflow layer isn't in platform.yaml."""
    from providers import PermissionLayer

    yaml_path = tmp_path / "platform.yaml"
    # The platform list happens to carry an identical prompt rule; the
    # layer match must NOT anchor on it.
    _write_platform(yaml_path, [_BASH_PROMPT, _READ, _TAIL])
    provider = _bootstrap(yaml_path)
    _wire_like_chat(provider, yaml_path, pattern="ls *")
    layer = PermissionLayer(
        name="bundle",
        rules=(Rule(what="kolsch/tools/bash", pattern="*", effect="prompt"),),
    )

    await provider.allow(
        "agent.ag_x", "kolsch/tools/bash", target="ls", layers=(layer,)
    )

    assert _disk_rules(yaml_path) == [
        _key(_BASH_PROMPT),
        _key(_READ),
        ("kolsch/tools/bash", "ls *", "allow"),
        _key(_TAIL),
    ]
