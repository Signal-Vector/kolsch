"""bug-159 — rule ids include `from`; unscoped ids are byte-identical.

Before bug-159 `derive_rule_id` ignored `from`, so a scoped rule and
its unscoped twin shared one id, and a scoped catch-all deny took the
protected `rl_default_deny`. Every id-keyed operation (`remove_rule`,
`get_rule`, `before=` / `after=`) then acted on whichever came first.

The pinned unscoped ids below were computed on the pre-bug-159 code;
they must never change (goldens carry `rl_fd8d8aa9` / `rl_5e8d840c`).
"""

from __future__ import annotations

from typing import Any

import pytest

from errors import AuthzProtectedRule
from providers.authz import (
    DEFAULT_DENY_RULE_ID,
    Rule,
    RuleBasedAuthorizationProvider,
    derive_rule_id,
)

A = "kolsch/agents/a"
_TAIL: dict[str, str] = {"what": "*", "pattern": "*", "effect": "deny"}


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((".*", ".*", "allow"), "rl_fd8d8aa9"),
        ((".*", "*|*", "allow"), "rl_5e8d840c"),
        ((".*", r"tool\.bash", "allow"), "rl_de710548"),
        ((".*", r"tool\.bash", "deny"), "rl_80f25458"),
        ((".*", "kolsch/tools/bash|*", "deny"), "rl_7992e624"),
        ((".*", "kolsch/tools/bash|ls *", "prompt"), "rl_b1ef0c99"),
        ((".*", "agent.execute.*|*", "allow"), "rl_51b34ba5"),
        (("^user$", ".*", "deny"), "rl_ec32df10"),
        ((".*", ".*", "deny"), DEFAULT_DENY_RULE_ID),
        ((".*", "*|*", "deny"), DEFAULT_DENY_RULE_ID),
    ],
)
def test_unscoped_ids_are_byte_identical(
    args: tuple[str, str, str], expected: str
) -> None:
    assert derive_rule_id(*args) == expected
    assert derive_rule_id(*args, from_=None) == expected


def test_scoped_id_differs_from_unscoped_and_by_scope() -> None:
    unscoped = derive_rule_id(".*", "kolsch/tools/bash|*", "deny")
    scoped_a = derive_rule_id(".*", "kolsch/tools/bash|*", "deny", from_=A)
    scoped_b = derive_rule_id(
        ".*", "kolsch/tools/bash|*", "deny", from_="kolsch/agents/b"
    )
    assert len({unscoped, scoped_a, scoped_b}) == 3
    assert scoped_a.startswith("rl_")
    assert len(scoped_a) == len(unscoped)


@pytest.mark.parametrize("what", [".*", "*|*"])
def test_scoped_catch_all_deny_never_gets_reserved_id(what: str) -> None:
    assert derive_rule_id(".*", what, "deny", from_=A) != DEFAULT_DENY_RULE_ID


def _twins(effect: str = "deny") -> list[dict[str, Any]]:
    unscoped = {"what": "kolsch/tools/bash", "pattern": "*", "effect": effect}
    return [unscoped, unscoped | {"from": A}, _TAIL]


async def test_yaml_twins_get_distinct_ids() -> None:
    provider = RuleBasedAuthorizationProvider(config={"rules": _twins()})
    ids = [r.id for r in await provider.list_rules()]
    assert ids[0] == "rl_7992e624"
    assert ids[1] != ids[0]
    assert ids[2] == DEFAULT_DENY_RULE_ID


async def test_add_rule_scoped_twin_gets_distinct_id() -> None:
    provider = RuleBasedAuthorizationProvider(config={"rules": [_twins()[0], _TAIL]})
    added = await provider.add_rule(
        Rule(what="kolsch/tools/bash", pattern="*", from_=A, effect="deny")
    )
    rules = await provider.list_rules()
    assert added.id != rules[0].id
    assert [r.id for r in rules].index(added.id) == 1


async def test_id_operations_act_on_the_scoped_rule() -> None:
    provider = RuleBasedAuthorizationProvider(config={"rules": _twins()})
    rules = await provider.list_rules()
    scoped_id = rules[1].id

    assert (await provider.get_rule(scoped_id)).from_ == A

    before = await provider.add_rule(
        Rule(what="kolsch/tools/ls", effect="allow"), before=scoped_id
    )
    assert [r.id for r in await provider.list_rules()].index(before.id) == 1
    after = await provider.add_rule(
        Rule(what="kolsch/tools/cat", effect="allow"), after=scoped_id
    )
    assert [r.id for r in await provider.list_rules()].index(after.id) == 3

    await provider.remove_rule(scoped_id)
    remaining = await provider.list_rules()
    assert [(r.what, r.from_) for r in remaining] == [
        ("kolsch/tools/bash", None),
        ("kolsch/tools/ls", None),
        ("kolsch/tools/cat", None),
        ("*", None),
    ]


async def test_scoped_catch_all_deny_is_an_ordinary_rule() -> None:
    scoped_tail = _TAIL | {"from": A}
    provider = RuleBasedAuthorizationProvider(
        config={"rules": [scoped_tail, {"what": "x.y", "effect": "allow"}, _TAIL]}
    )
    rules = await provider.list_rules()
    assert rules[0].id != DEFAULT_DENY_RULE_ID
    # `_tail_index` anchors on the real (unscoped) tail, not the scoped one.
    assert provider._tail_index() == 2
    added = await provider.add_rule(Rule(what="kolsch/tools/ls", effect="allow"))
    assert [r.id for r in await provider.list_rules()].index(added.id) == 2

    await provider.remove_rule(rules[0].id)
    assert all(r.from_ is None for r in await provider.list_rules())
    with pytest.raises(AuthzProtectedRule):
        await provider.remove_rule(DEFAULT_DENY_RULE_ID)
