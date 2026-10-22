"""bug-162 — `cli.permissions.mutate_platform_rules`, the locked rules writer.

Every op is applied to the rules currently on disk, inserts or removes
exactly one round-trip entry (comments and other keys kept), and runs
under `platform_yaml_lock`. Every failure leaves the file byte-identical
and never creates it.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

import cli.permissions as permissions_mod
from assets.platform_config import (
    default_platform_payload,
    platform_yaml_lock,
    platform_yaml_lock_path,
)
from cli.permissions import (
    InsertRule,
    RemoveRule,
    UpsertRule,
    mutate_platform_rules,
)
from errors import (
    AuthzPatternInvalid,
    AuthzProtectedRule,
    AuthzRuleInvalid,
    AuthzRuleNotFound,
    AuthzRulesPersistFailed,
)
from kolsch._yamlio import dump, load
from providers import DEFAULT_DENY_RULE_ID, Rule

_SRC = Path(__file__).resolve().parents[3] / "src"

_PLATFORM = """\
# header comment — kept by every write
authorization:
  variant: rule_based
  config:
    rules:
      # leading comment before the first rule
      - what: kolsch/tools/read_file  # inline comment on read_file
        pattern: '*'
        effect: allow
      - what: kolsch/tools/bash
        pattern: '*'
        effect: prompt
      # comment above the tail
      - what: '*'
        pattern: '*'
        effect: deny
defaults:
  model: mock/echo  # inline comment on a non-rule key
file_access:
  allowed_paths: [.]
# trailing scaffold block
# hooks:
#   - event: tool.call.completed
"""

_COMMENT_LINES = [
    line.strip()[line.strip().index("#") :]
    for line in _PLATFORM.splitlines()
    if "#" in line
]

Key = tuple[str, str, str]
_READ: Key = ("kolsch/tools/read_file", "*", "allow")
_BASH_PROMPT: Key = ("kolsch/tools/bash", "*", "prompt")
_TAIL: Key = ("*", "*", "deny")


def _ws(tmp_path: Path, text: str = _PLATFORM) -> Path:
    yaml_path = tmp_path / "platform.yaml"
    yaml_path.write_text(text, encoding="utf-8")
    return yaml_path


def _payload(yaml_path: Path) -> dict[str, Any]:
    payload = load(yaml_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _keys(yaml_path: Path) -> list[Key]:
    rules = _payload(yaml_path)["authorization"]["config"]["rules"]
    return [(r["what"], r.get("pattern", "*"), r["effect"]) for r in rules]


def _rule(what: str, pattern: str = "*", effect: str = "allow", **kw: Any) -> Rule:
    return Rule(what=what, pattern=pattern, effect=effect, **kw)


def _insert(
    yaml_path: Path, rule: Rule, **kw: Any
) -> permissions_mod.RulesMutationResult:
    return mutate_platform_rules(yaml_path, InsertRule(rule, **kw), catalog=None)


# ---------------------------------------------------------------------------
# Insert positions
# ---------------------------------------------------------------------------


def test_anchor_or_tail_inserts_before_disk_copy_of_anchor(tmp_path: Path) -> None:
    yaml_path = _ws(tmp_path)
    anchor = _rule("kolsch/tools/bash", effect="prompt")
    new = _rule("kolsch/tools/bash", "ls *")

    result = _insert(yaml_path, new, anchor=anchor)

    assert _keys(yaml_path) == [
        _READ,
        ("kolsch/tools/bash", "ls *", "allow"),
        _BASH_PROMPT,
        _TAIL,
    ]
    assert result.written and result.inserted and not result.removed
    assert result.position == 1
    assert result.rule is not None and result.rule.pattern == "ls *" and result.rule.id
    assert [r.what for r in result.disk_rules_before] == [
        "kolsch/tools/read_file",
        "kolsch/tools/bash",
        "*",
    ]
    assert len(result.disk_rules_after) == 4
    assert result.source_map[1][1] > 0  # feat-041 breadcrumbs for the new file


def test_anchor_or_tail_falls_back_to_tail_when_anchor_not_on_disk(
    tmp_path: Path,
) -> None:
    yaml_path = _ws(tmp_path)
    gone = _rule("kolsch/tools/write_file", effect="prompt")  # e.g. a bundle-layer rule

    _insert(yaml_path, _rule("kolsch/tools/write_file", "notes/*"), anchor=gone)

    assert _keys(yaml_path) == [
        _READ,
        _BASH_PROMPT,
        ("kolsch/tools/write_file", "notes/*", "allow"),
        _TAIL,
    ]


def test_anchor_matches_structurally_including_from(tmp_path: Path) -> None:
    yaml_path = _ws(
        tmp_path,
        _PLATFORM.replace(
            "      - what: kolsch/tools/bash\n",
            "      - what: kolsch/tools/bash\n"
            "        pattern: '*'\n"
            "        from: kolsch/agents/butler\n"
            "        effect: prompt\n"
            "      - what: kolsch/tools/bash\n",
        ),
    )
    # Same derived id as the scoped rule (ids ignore `from:`), but the
    # anchor is the unscoped one — it must land before index 2, not 1.
    _insert(
        yaml_path,
        _rule("kolsch/tools/bash", "ls *"),
        anchor=_rule("kolsch/tools/bash", effect="prompt"),
    )

    rules = _payload(yaml_path)["authorization"]["config"]["rules"]
    assert [(r["what"], r["pattern"], r.get("from")) for r in rules[1:4]] == [
        ("kolsch/tools/bash", "*", "kolsch/agents/butler"),
        ("kolsch/tools/bash", "ls *", None),
        ("kolsch/tools/bash", "*", None),
    ]


def test_tail_position_without_tail_appends(tmp_path: Path) -> None:
    yaml_path = _ws(
        tmp_path,
        _PLATFORM.replace(
            "      - what: '*'\n        pattern: '*'\n        effect: deny\n", ""
        ),
    )

    result = _insert(yaml_path, _rule("kolsch/tools/list_files"), position="tail")

    assert _keys(yaml_path) == [
        _READ,
        _BASH_PROMPT,
        ("kolsch/tools/list_files", "*", "allow"),
    ]
    assert result.position == 2


def test_tail_found_by_shape_not_id(tmp_path: Path) -> None:
    """A `from:`-scoped catch-all deny shares the tail's id but is not the tail."""
    scoped = (
        "      - what: '*'\n"
        "        pattern: '*'\n"
        "        from: kolsch/agents/butler\n"
        "        effect: deny\n"
    )
    yaml_path = _ws(
        tmp_path, _PLATFORM.replace("      # comment above the tail\n", scoped)
    )

    _insert(yaml_path, _rule("kolsch/tools/list_files"), position="tail")

    rules = _payload(yaml_path)["authorization"]["config"]["rules"]
    assert [(r["what"], r.get("from")) for r in rules] == [
        ("kolsch/tools/read_file", None),
        ("kolsch/tools/bash", None),
        ("*", "kolsch/agents/butler"),
        ("kolsch/tools/list_files", None),
        ("*", None),
    ]


def test_legacy_tail_shape_is_the_tail(tmp_path: Path) -> None:
    yaml_path = _ws(
        tmp_path,
        _PLATFORM.replace(
            "      - what: '*'\n        pattern: '*'\n        effect: deny\n",
            "      - id: rl_custom_tail\n        who: '.*'\n        what: '.*'\n        effect: deny\n",
        ),
    )

    _insert(yaml_path, _rule("kolsch/tools/list_files"), position="tail")

    assert [k[0] for k in _keys(yaml_path)] == [
        "kolsch/tools/read_file",
        "kolsch/tools/bash",
        "kolsch/tools/list_files",
        ".*",
    ]


def test_first_before_after(tmp_path: Path) -> None:
    yaml_path = _ws(tmp_path)
    first = _insert(yaml_path, _rule("kolsch/tools/a"), position="first")
    assert first.position == 0
    bash_id = next(
        r.id for r in first.disk_rules_after if r.what == "kolsch/tools/bash"
    )

    _insert(yaml_path, _rule("kolsch/tools/b"), position="before", ref_id=bash_id)
    _insert(yaml_path, _rule("kolsch/tools/c"), position="after", ref_id=bash_id)

    assert [k[0] for k in _keys(yaml_path)] == [
        "kolsch/tools/a",
        "kolsch/tools/read_file",
        "kolsch/tools/b",
        "kolsch/tools/bash",
        "kolsch/tools/c",
        "*",
    ]


@pytest.mark.parametrize("position", ["before", "after"])
def test_missing_ref_id_raises_rule_invalid_and_leaves_file(
    tmp_path: Path, position: str
) -> None:
    yaml_path = _ws(tmp_path)
    before = yaml_path.read_bytes()

    with pytest.raises(AuthzRuleInvalid, match="rl_nope"):
        _insert(yaml_path, _rule("kolsch/tools/a"), position=position, ref_id="rl_nope")

    assert yaml_path.read_bytes() == before


def test_malformed_insert_op_is_rejected(tmp_path: Path) -> None:
    yaml_path = _ws(tmp_path)
    with pytest.raises(AuthzRuleInvalid):
        _insert(yaml_path, _rule("kolsch/tools/a"), position="before")
    with pytest.raises(AuthzRuleInvalid):
        _insert(yaml_path, _rule("kolsch/tools/a"), position="first", ref_id="rl_x")


def test_invalid_new_rule_raises_its_own_error_and_leaves_file(tmp_path: Path) -> None:
    yaml_path = _ws(tmp_path)
    before = yaml_path.read_bytes()

    with pytest.raises(AuthzPatternInvalid):
        _insert(yaml_path, _rule("kolsch/tools/bash", "^ls$"))

    assert yaml_path.read_bytes() == before


# ---------------------------------------------------------------------------
# D7 — identical rule already in effect
# ---------------------------------------------------------------------------


def test_identical_rule_already_on_disk_is_a_no_op(tmp_path: Path) -> None:
    yaml_path = _ws(tmp_path)
    before = yaml_path.read_bytes()
    mtime = yaml_path.stat().st_mtime_ns

    result = _insert(yaml_path, _rule("kolsch/tools/read_file"))

    assert not result.written and not result.inserted
    assert result.position == 0
    assert result.rule is not None and result.rule.what == "kolsch/tools/read_file"
    assert result.disk_rules_after == result.disk_rules_before
    assert yaml_path.read_bytes() == before
    assert yaml_path.stat().st_mtime_ns == mtime


def test_identical_rule_only_after_the_slot_still_inserts(tmp_path: Path) -> None:
    """A duplicate behind the tail is dead — inserting before the tail is not a no-op."""
    yaml_path = _ws(
        tmp_path,
        _PLATFORM.replace(
            "defaults:",
            "      - what: kolsch/tools/x\n        pattern: '*'\n        effect: allow\ndefaults:",
        ),
    )

    result = _insert(yaml_path, _rule("kolsch/tools/x"))

    assert result.inserted and result.position == 2
    assert _keys(yaml_path).count(("kolsch/tools/x", "*", "allow")) == 2


# ---------------------------------------------------------------------------
# Upsert / remove
# ---------------------------------------------------------------------------


def test_upsert_replaces_same_scope_and_lands_before_tail(tmp_path: Path) -> None:
    yaml_path = _ws(tmp_path)

    result = mutate_platform_rules(
        yaml_path, UpsertRule(_rule("kolsch/tools/bash", effect="deny")), catalog=None
    )

    assert _keys(yaml_path) == [_READ, ("kolsch/tools/bash", "*", "deny"), _TAIL]
    assert result.replaced_position == 1 and result.removed and result.inserted
    assert result.position == 1


def test_upsert_keeps_differently_scoped_rule(tmp_path: Path) -> None:
    yaml_path = _ws(tmp_path)

    mutate_platform_rules(
        yaml_path,
        UpsertRule(
            _rule("kolsch/tools/bash", effect="allow", from_="kolsch/agents/butler")
        ),
        catalog=None,
    )

    rules = _payload(yaml_path)["authorization"]["config"]["rules"]
    assert [(r["what"], r["effect"], r.get("from")) for r in rules] == [
        ("kolsch/tools/read_file", "allow", None),
        ("kolsch/tools/bash", "prompt", None),
        ("kolsch/tools/bash", "allow", "kolsch/agents/butler"),
        ("*", "deny", None),
    ]


def test_upsert_of_the_tail_is_protected(tmp_path: Path) -> None:
    yaml_path = _ws(tmp_path)
    before = yaml_path.read_bytes()

    with pytest.raises(AuthzProtectedRule):
        mutate_platform_rules(
            yaml_path, UpsertRule(_rule("*", effect="allow")), catalog=None
        )

    assert yaml_path.read_bytes() == before


def test_remove_by_match_drops_only_that_rule(tmp_path: Path) -> None:
    yaml_path = _ws(tmp_path)

    result = mutate_platform_rules(
        yaml_path,
        RemoveRule(match=_rule("kolsch/tools/bash", effect="allow")),
        catalog=None,
    )

    assert _keys(yaml_path) == [_READ, _TAIL]
    assert result.removed and result.position == 1
    assert result.rule is not None and result.rule.effect == "prompt"


def test_remove_by_match_without_match_writes_nothing(tmp_path: Path) -> None:
    yaml_path = _ws(tmp_path)
    before = yaml_path.read_bytes()

    result = mutate_platform_rules(
        yaml_path, RemoveRule(match=_rule("kolsch/tools/nope")), catalog=None
    )

    assert not result.written and not result.removed and result.position is None
    assert yaml_path.read_bytes() == before


def test_remove_by_id(tmp_path: Path) -> None:
    yaml_path = _ws(tmp_path)
    rule_id = (
        _insert(yaml_path, _rule("kolsch/tools/z"), position="first")
        .disk_rules_after[0]
        .id
    )

    mutate_platform_rules(yaml_path, RemoveRule(rule_id=rule_id), catalog=None)

    assert _keys(yaml_path) == [_READ, _BASH_PROMPT, _TAIL]


def test_remove_unknown_id_raises_not_found(tmp_path: Path) -> None:
    yaml_path = _ws(tmp_path)
    before = yaml_path.read_bytes()

    with pytest.raises(AuthzRuleNotFound):
        mutate_platform_rules(yaml_path, RemoveRule(rule_id="rl_nope"), catalog=None)

    assert yaml_path.read_bytes() == before


@pytest.mark.parametrize(
    "op",
    [
        RemoveRule(rule_id=DEFAULT_DENY_RULE_ID),
        RemoveRule(match=Rule(what="*", pattern="*", effect="deny")),
    ],
)
def test_removing_the_tail_is_refused_byte_identical(
    tmp_path: Path, op: RemoveRule
) -> None:
    yaml_path = _ws(tmp_path)
    before = yaml_path.read_bytes()

    with pytest.raises(AuthzProtectedRule):
        mutate_platform_rules(yaml_path, op, catalog=None)

    assert yaml_path.read_bytes() == before


def test_remove_needs_exactly_one_selector(tmp_path: Path) -> None:
    yaml_path = _ws(tmp_path)
    with pytest.raises(AuthzRuleInvalid):
        mutate_platform_rules(yaml_path, RemoveRule(), catalog=None)
    with pytest.raises(AuthzRuleInvalid):
        mutate_platform_rules(
            yaml_path,
            RemoveRule(match=_rule("a/tools/b"), rule_id="rl_x"),
            catalog=None,
        )


# ---------------------------------------------------------------------------
# File fidelity (D2)
# ---------------------------------------------------------------------------


def test_round_trip_keeps_every_comment_and_other_key(tmp_path: Path) -> None:
    yaml_path = _ws(tmp_path)
    before = _payload(yaml_path)

    _insert(yaml_path, _rule("kolsch/tools/list_files"))
    mutate_platform_rules(
        yaml_path, RemoveRule(match=_rule("kolsch/tools/bash")), catalog=None
    )

    text = yaml_path.read_text(encoding="utf-8")
    missing = [c for c in _COMMENT_LINES if c not in text]
    assert not missing, f"comments dropped: {missing}\n---\n{text}"
    after = _payload(yaml_path)
    assert {k: v for k, v in after.items() if k != "authorization"} == {
        k: v for k, v in before.items() if k != "authorization"
    }
    assert after["authorization"]["variant"] == "rule_based"
    assert _keys(yaml_path) == [_READ, ("kolsch/tools/list_files", "*", "allow"), _TAIL]


def test_untouched_lines_are_byte_identical(tmp_path: Path) -> None:
    """Only the inserted entry's lines are new; every original line is kept in order."""
    yaml_path = _ws(tmp_path)

    _insert(yaml_path, _rule("kolsch/tools/list_files"))

    new_lines = yaml_path.read_text(encoding="utf-8").splitlines()
    at = new_lines.index("      - what: kolsch/tools/list_files")
    assert new_lines[at : at + 3] == [
        "      - what: kolsch/tools/list_files",
        "        pattern: '*'",
        "        effect: allow",
    ]
    # The comment that sat above the tail stays directly above it; every
    # other line is unchanged and in order.
    assert new_lines[at + 3] == "      # comment above the tail"
    assert new_lines[:at] + new_lines[at + 3 :] == _PLATFORM.splitlines()


def test_kolsch_generated_file_round_trips_unchanged_but_for_new_entry(
    tmp_path: Path,
) -> None:
    """The `kolsch setup` layout (dash at key indent) is kept as written."""
    original = dump(default_platform_payload())
    yaml_path = _ws(tmp_path, original)

    result = _insert(yaml_path, _rule("kolsch/tools/list_files", "docs/*"))

    assert result.written
    new_lines = yaml_path.read_text(encoding="utf-8").splitlines()
    at = new_lines.index("    - what: kolsch/tools/list_files")
    del new_lines[at : at + 3]
    assert new_lines == original.splitlines()


def test_removal_keeps_comment_above_next_entry_and_drops_own_inline(
    tmp_path: Path,
) -> None:
    yaml_path = _ws(
        tmp_path,
        _PLATFORM.replace(
            "        effect: prompt\n",
            "        effect: prompt  # inline on the removed rule\n",
        ),
    )

    mutate_platform_rules(
        yaml_path, RemoveRule(match=_rule("kolsch/tools/bash")), catalog=None
    )

    lines = yaml_path.read_text(encoding="utf-8").splitlines()
    assert "inline on the removed rule" not in "\n".join(lines)
    at = lines.index("      # comment above the tail")
    assert lines[at + 1] == "      - what: '*'"
    assert lines[at - 1] == "        effect: allow"


def test_removing_the_first_entry_keeps_comments_around_it(tmp_path: Path) -> None:
    yaml_path = _ws(
        tmp_path,
        _PLATFORM.replace(
            "      - what: kolsch/tools/bash\n",
            "      # comment above bash\n      - what: kolsch/tools/bash\n",
        ),
    )

    mutate_platform_rules(
        yaml_path, RemoveRule(match=_rule("kolsch/tools/read_file")), catalog=None
    )

    text = yaml_path.read_text(encoding="utf-8")
    assert "inline comment on read_file" not in text
    lines = text.splitlines()
    assert lines.index("      # leading comment before the first rule") < lines.index(
        "      # comment above bash"
    )
    assert lines[lines.index("      # comment above bash") + 1] == (
        "      - what: kolsch/tools/bash"
    )
    assert _keys(yaml_path) == [_BASH_PROMPT, _TAIL]


def test_file_mode_is_preserved(tmp_path: Path) -> None:
    yaml_path = _ws(tmp_path)
    yaml_path.chmod(0o640)

    _insert(yaml_path, _rule("kolsch/tools/list_files"))

    assert (yaml_path.stat().st_mode & 0o777) == 0o640
    assert not list(tmp_path.glob("platform.yaml.tmp.*"))


@pytest.mark.parametrize(
    ("text", "expected_rules"),
    [
        ("defaults:\n  model: mock/echo\n", 1),
        ("authorization:\n  variant: rule_based\n", 1),
        ("authorization:\n  variant: rule_based\n  config:\n    rules:\n", 1),
        ("authorization:\n  variant: rule_based\n  config:\n    rules: []\n", 1),
    ],
)
def test_absent_or_null_rules_section_is_created(
    tmp_path: Path, text: str, expected_rules: int
) -> None:
    yaml_path = _ws(tmp_path, text)

    _insert(yaml_path, _rule("kolsch/tools/list_files"))

    assert (
        _keys(yaml_path) == [("kolsch/tools/list_files", "*", "allow")][:expected_rules]
    )
    assert "{" not in yaml_path.read_text(encoding="utf-8")  # block style


# ---------------------------------------------------------------------------
# Failure modes — bytes unchanged, never created
# ---------------------------------------------------------------------------


def _assert_persist_failed(
    yaml_path: Path, reason: str, **kw: Any
) -> AuthzRulesPersistFailed:
    before = yaml_path.read_bytes() if yaml_path.exists() else None
    with pytest.raises(AuthzRulesPersistFailed) as info:
        _insert(yaml_path, _rule("kolsch/tools/list_files"), **kw)
    err = info.value
    assert err.code == "authz.rules_persist_failed"
    assert err.detail["reason"] == reason
    assert err.detail["path"] == str(yaml_path)
    assert err.detail["detail"]
    if before is None:
        assert not yaml_path.exists()
    else:
        assert yaml_path.read_bytes() == before
    assert not list(yaml_path.parent.glob("platform.yaml.tmp.*"))
    return err


def test_missing_file_is_not_created(tmp_path: Path) -> None:
    _assert_persist_failed(tmp_path / "platform.yaml", "missing")
    assert not (tmp_path / ".platform").exists()


@pytest.mark.parametrize(
    "text",
    [
        "authorization: [unclosed\n",
        "- just\n- a list\n",
        "",
        "authorization: nope\n",
        "authorization:\n  config: [1]\n",
        "authorization:\n  config:\n    rules: {what: x}\n",
        "authorization:\n  config:\n    rules: not-a-list\n",
    ],
)
def test_unparseable_shapes(tmp_path: Path, text: str) -> None:
    _assert_persist_failed(_ws(tmp_path, text), "unparseable")


@pytest.mark.parametrize(
    "bad_rule",
    [
        "      - what: kolsch/tools/bash\n        pattern: '^rm$'\n        effect: deny\n",
        "      - what: kolsch/tools/bash\n        effect: maybe\n",
        "      - just-a-string\n",
    ],
)
def test_invalid_disk_rules_refuse_to_write(tmp_path: Path, bad_rule: str) -> None:
    yaml_path = _ws(
        tmp_path, _PLATFORM.replace("      # comment above the tail\n", bad_rule)
    )
    _assert_persist_failed(yaml_path, "invalid_rules")


def test_write_failure_leaves_file_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _ws(tmp_path)

    def boom(*_args: object) -> None:
        msg = "simulated rename failure"
        raise OSError(msg)

    monkeypatch.setattr(permissions_mod.os, "replace", boom)
    err = _assert_persist_failed(yaml_path, "write_failed")
    assert "simulated rename failure" in err.detail["detail"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_read_only_directory_is_write_failed(tmp_path: Path) -> None:
    yaml_path = _ws(tmp_path)
    (tmp_path / ".platform").mkdir()
    tmp_path.chmod(0o555)
    try:
        _assert_persist_failed(yaml_path, "write_failed")
    finally:
        tmp_path.chmod(0o755)


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


def test_write_happens_while_the_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _ws(tmp_path)
    real_write = permissions_mod._atomic_write_text
    probes: list[bool] = []

    def probing_write(path: Path, text: str) -> None:
        with platform_yaml_lock_path(tmp_path).open("ab") as fd:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                probes.append(True)
            else:
                probes.append(False)
        real_write(path, text)

    monkeypatch.setattr(permissions_mod, "_atomic_write_text", probing_write)
    _insert(yaml_path, _rule("kolsch/tools/list_files"))

    assert probes == [True], "platform.yaml was written without the rules lock held"
    assert (
        platform_yaml_lock_path(tmp_path)
        == tmp_path / ".platform" / "platform.yaml.lock"
    )


def _spawn(code: str) -> subprocess.Popen[str]:
    env = {
        **os.environ,
        "PYTHONPATH": f"{_SRC}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
    }
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(code)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_lock_held_by_another_process_times_out_with_bytes_unchanged(
    tmp_path: Path,
) -> None:
    yaml_path = _ws(tmp_path)
    ready = tmp_path / "ready"
    holder = _spawn(
        f"""
        import time
        from pathlib import Path
        from assets.platform_config import platform_yaml_lock
        with platform_yaml_lock(Path({str(tmp_path)!r})):
            Path({str(ready)!r}).write_text("x")
            time.sleep(30)
        """
    )
    try:
        deadline = time.monotonic() + 20
        while not ready.exists():
            assert holder.poll() is None, holder.communicate()
            assert time.monotonic() < deadline, "lock holder never started"
            time.sleep(0.05)

        start = time.monotonic()
        err = _assert_persist_failed(yaml_path, "lock_timeout")
        elapsed = time.monotonic() - start
    finally:
        holder.kill()
        holder.communicate()
    assert 4.5 <= elapsed <= 6.0, elapsed
    assert "platform.yaml.lock" in err.detail["detail"]


def test_lock_is_released_after_use(tmp_path: Path) -> None:
    with platform_yaml_lock(tmp_path, timeout_s=0.5):
        pass
    with platform_yaml_lock(tmp_path, timeout_s=0.5):
        pass


def test_concurrent_processes_each_insert_once(tmp_path: Path) -> None:
    yaml_path = _ws(tmp_path)
    go = tmp_path / "go"
    count = 8
    workers = [
        _spawn(
            f"""
            import time
            from pathlib import Path
            from cli.permissions import InsertRule, mutate_platform_rules
            from providers import Rule
            go = Path({str(go)!r})
            while not go.exists():
                time.sleep(0.005)
            mutate_platform_rules(
                Path({str(yaml_path)!r}),
                InsertRule(Rule(what="kolsch/tools/w{i}", pattern="*", effect="allow")),
                catalog=None,
                lock_timeout_s=60,
            )
            """
        )
        for i in range(count)
    ]
    go.write_text("x")
    for worker in workers:
        _out, err = worker.communicate(timeout=120)
        assert worker.returncode == 0, err

    keys = _keys(yaml_path)
    for i in range(count):
        assert keys.count((f"kolsch/tools/w{i}", "*", "allow")) == 1, keys
    assert keys[:2] == [_READ, _BASH_PROMPT] and keys[-1] == _TAIL
    assert len(keys) == 3 + count
    text = yaml_path.read_text(encoding="utf-8")
    assert not [c for c in _COMMENT_LINES if c not in text]
