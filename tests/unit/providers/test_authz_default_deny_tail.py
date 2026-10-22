"""bug-152 — `inspect_default_deny_tail` + `describe_default_deny_tail_problem`.

Pure classification of where a raw `authorization.config.rules` list
carries the default-deny tail (by shape, never by id), and the shared
user-facing text the bootstrap WARNING and `kolsch doctor` print.
"""

from __future__ import annotations

import pytest

from providers import (
    DefaultDenyTailStatus,
    describe_default_deny_tail_problem,
    inspect_default_deny_tail,
)

_TAIL = {"what": "*", "pattern": "*", "effect": "deny"}
_LEGACY_TAIL = {"who": ".*", "what": ".*", "effect": "deny"}
_LEGACY_ALLOW_ALL = {"who": ".*", "what": ".*", "effect": "allow"}
_FIX = "kolsch permissions add --what '*' --pattern '*' --effect deny"


def _allow(ref: str) -> dict[str, str]:
    return {"what": ref, "pattern": "*", "effect": "allow"}


def test_canonical_tail_last() -> None:
    status = inspect_default_deny_tail([_allow("kolsch/tools/bash"), _TAIL])
    assert status == DefaultDenyTailStatus(
        state="last",
        index=1,
        rule_count=2,
        catch_all_allow_index=None,
        tail_shape="canonical",
    )
    assert describe_default_deny_tail_problem(status) is None


def test_canonical_tail_with_pattern_absent_is_last() -> None:
    status = inspect_default_deny_tail([{"what": "*", "effect": "deny"}])
    assert status.state == "last"
    assert status.tail_shape == "canonical"


def test_legacy_tail_last() -> None:
    status = inspect_default_deny_tail([_allow("kolsch/tools/bash"), _LEGACY_TAIL])
    assert status.state == "last"
    assert status.index == 1
    assert status.tail_shape == "legacy"
    assert describe_default_deny_tail_problem(status) is None


def test_tail_not_last_names_position_and_dead_rules() -> None:
    rules = [
        _allow("a/tools/x"),
        _allow("a/tools/y"),
        _TAIL,
        _allow("a/tools/z"),
        _allow("a/tools/w"),
    ]
    status = inspect_default_deny_tail(rules)
    assert status.state == "not_last"
    assert status.index == 2
    assert status.rule_count == 5
    text = describe_default_deny_tail_problem(status)
    assert text is not None
    assert "position 3 of 5" in text
    assert "the 2 rules after it never match" in text
    assert '{what: "*", pattern: "*", effect: deny}' in text
    assert "end of `authorization.config.rules`" in text
    assert "kolsch doctor" in text
    assert "--sync-bundle" not in text


def test_tail_not_last_singular_wording() -> None:
    status = inspect_default_deny_tail([_TAIL, _allow("a/tools/x")])
    text = describe_default_deny_tail_problem(status)
    assert text is not None
    assert "the 1 rule after it never matches" in text


def test_legacy_tail_not_last_names_legacy_shape() -> None:
    status = inspect_default_deny_tail([_LEGACY_TAIL, _allow("a/tools/x")])
    assert status.state == "not_last"
    text = describe_default_deny_tail_problem(status)
    assert text is not None
    assert '{who: ".*", what: ".*", effect: deny}' in text


def test_first_tail_wins_when_duplicated() -> None:
    status = inspect_default_deny_tail([_TAIL, _allow("a/tools/x"), _TAIL])
    assert status.state == "not_last"
    assert status.index == 0


def test_no_tail_is_missing() -> None:
    status = inspect_default_deny_tail([_allow("a/tools/x"), _allow("a/tools/y")])
    assert status.state == "missing"
    assert status.index is None
    assert status.tail_shape is None
    assert status.catch_all_allow_index is None
    text = describe_default_deny_tail_problem(status)
    assert text is not None
    assert "no default-deny tail" in text
    assert "ask for approval in `kolsch chat` and be denied everywhere else" in text
    assert _FIX in text
    assert "kolsch doctor" in text
    assert "--sync-bundle" not in text


def test_empty_list_is_missing() -> None:
    status = inspect_default_deny_tail([])
    assert status.state == "missing"
    assert status.rule_count == 0


def test_scoped_deny_is_not_a_tail() -> None:
    scoped = {**_TAIL, "from": "kolsch/agents/x"}
    status = inspect_default_deny_tail([_allow("a/tools/x"), scoped])
    assert status.state == "missing"


def test_scoped_deny_via_from_underscore_is_not_a_tail() -> None:
    scoped = {**_TAIL, "from_": "kolsch/agents/x"}
    assert inspect_default_deny_tail([scoped]).state == "missing"


def test_explicit_from_none_is_still_a_tail() -> None:
    assert inspect_default_deny_tail([{**_TAIL, "from": None}]).state == "last"


def test_id_alone_does_not_make_a_tail() -> None:
    # A legacy rule may carry any caller-supplied id; shape decides.
    fake = {
        "id": "rl_default_deny",
        "who": ".*",
        "what": "^tool\\.bash$",
        "effect": "deny",
    }
    assert inspect_default_deny_tail([fake]).state == "missing"


@pytest.mark.parametrize(
    "rule",
    [
        {"what": "*", "pattern": "/tmp/*", "effect": "deny"},
        {"what": "kolsch/tools/bash", "pattern": "*", "effect": "deny"},
        {"who": "agent\\..*", "what": ".*", "effect": "deny"},
        {"who": ".*", "what": "*", "effect": "deny"},
        {"what": "*", "pattern": "*", "effect": "prompt"},
    ],
)
def test_non_catch_all_shapes_are_not_tails(rule: dict[str, str]) -> None:
    assert inspect_default_deny_tail([rule]).state == "missing"


def test_catch_all_allow_only_is_missing_with_allow_index() -> None:
    status = inspect_default_deny_tail([_LEGACY_ALLOW_ALL])
    assert status.state == "missing"
    assert status.catch_all_allow_index == 0
    text = describe_default_deny_tail_problem(status)
    assert text is not None
    assert (
        "catch-all allow rule at position 1 allows every action no earlier rule denies"
        in text
    )
    assert "ask for approval" not in text
    assert _FIX in text
    assert "kolsch doctor" in text
    assert "--sync-bundle" not in text


def test_canonical_catch_all_allow_detected() -> None:
    status = inspect_default_deny_tail(
        [_allow("a/tools/x"), {"what": "*", "effect": "allow"}]
    )
    assert status.catch_all_allow_index == 1


def test_catch_all_allow_before_tail_recorded_but_state_last() -> None:
    # F4 — recorded on the status, not warned about.
    status = inspect_default_deny_tail([_LEGACY_ALLOW_ALL, _TAIL])
    assert status.state == "last"
    assert status.catch_all_allow_index == 0
    assert describe_default_deny_tail_problem(status) is None


def test_catch_all_allow_after_tail_not_reported() -> None:
    status = inspect_default_deny_tail([_TAIL, _LEGACY_ALLOW_ALL])
    assert status.state == "not_last"
    assert status.catch_all_allow_index is None


def test_non_mapping_entries_skipped_but_keep_index() -> None:
    status = inspect_default_deny_tail(["junk", None, 3, _TAIL])
    assert status.state == "last"
    assert status.index == 3
    assert status.rule_count == 4
    assert inspect_default_deny_tail(["junk", _TAIL, 7]).state == "not_last"


def test_owner_shaped_list_is_missing_without_catch_all_allow() -> None:
    rules: list[object] = [
        {
            "id": "rl_281a0511",
            "who": ".*",
            "what": "^agent\\.execute\\.ag_butler$",
            "effect": "allow",
        },
        *[_allow(f"kolsch/tools/t{i}") for i in range(27)],
        _allow("autoply/tools/render_resume"),
    ]
    status = inspect_default_deny_tail(rules)
    assert status.state == "missing"
    assert status.rule_count == 29
    assert status.catch_all_allow_index is None
    text = describe_default_deny_tail_problem(status)
    assert text is not None
    assert "ask for approval in `kolsch chat`" in text
