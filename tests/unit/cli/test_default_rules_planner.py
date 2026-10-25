"""bug-146 Test 3 — the starter-rule manifest and the pure upgrade planner.

No filesystem, no stdio: `plan_default_rule_additions` and
`apply_default_rule_additions` are pure functions of their arguments.
"""

from __future__ import annotations

import copy
from collections import Counter

from assets.default_rules import STARTER_RULES, workspace_only_default_rules
from cli._migrations.default_rules import (
    apply_default_rule_additions,
    plan_default_rule_additions,
)

# Frozen copy of the 13-rule list `kolsch setup` wrote before bug-146.
_PRE_BUG_146_DEFAULTS = [
    {"what": "agent.execute.*", "pattern": "*", "effect": "allow"},
    {"what": "kolsch/tools/read_file", "pattern": "*", "effect": "allow"},
    {"what": "kolsch/tools/list_files", "pattern": "*", "effect": "allow"},
    {"what": "kolsch/tools/write_file", "pattern": "*", "effect": "prompt"},
    {"what": "kolsch/tools/bash", "pattern": "*", "effect": "prompt"},
    {"what": "kolsch/tools/fetch_url", "pattern": "*", "effect": "prompt"},
    {"what": "kolsch/tools/browse_url", "pattern": "*", "effect": "prompt"},
    {"what": "kolsch/tools/jev_decide", "pattern": "*", "effect": "prompt"},
    {"what": "kolsch/tools/run_workflow", "pattern": "*", "effect": "prompt"},
    {"what": "kolsch/tools/workflow_status", "pattern": "*", "effect": "allow"},
    {"what": "kolsch/tools/cancel_workflow", "pattern": "*", "effect": "allow"},
    {"what": "kolsch/tools/workflow_inspect", "pattern": "*", "effect": "allow"},
    {"what": "*", "pattern": "*", "effect": "deny"},
]
_TAIL = {"what": "*", "pattern": "*", "effect": "deny"}


def _owner_shaped(*, drop: tuple[str, ...] = ("jev_decide",)) -> list[object]:
    """Every default except `drop` (as user allows), a legacy rule, no tail."""
    rules: list[object] = [
        {
            "id": "rl_b",
            "who": ".*",
            "what": r"^agent\.execute\.ag_butler$",
            "effect": "allow",
        }
    ]
    for entry in STARTER_RULES:
        if entry.tail or entry.key in drop:
            continue
        rules.append({"what": entry.what, "pattern": "*", "effect": "allow"})
    return rules


def _keys(entries: tuple[object, ...]) -> list[str]:
    return [e.key for e in entries]


def test_manifest_derives_the_pre_bug_146_list() -> None:
    assert workspace_only_default_rules() == _PRE_BUG_146_DEFAULTS
    assert [e.tail for e in STARTER_RULES] == [False] * 12 + [True]
    assert len({e.key for e in STARTER_RULES}) == len(STARTER_RULES)
    assert all(e.since and e.rationale for e in STARTER_RULES)


def test_a_owner_shape_proposes_only_jev_decide_at_end() -> None:
    rules = _owner_shaped()
    plan = plan_default_rule_additions(rules, STARTER_RULES, {})
    assert _keys(plan.proposals) == ["jev_decide"]
    assert plan.tail_present is False
    assert plan.insertion_index == len(rules)
    assert plan.report_only == ()
    assert "fetch_url" in plan.present
    assert "default_deny_tail" not in plan.present


def test_b_new_shape_tail_index() -> None:
    rules = [*_owner_shaped(), dict(_TAIL)]
    plan = plan_default_rule_additions(rules, STARTER_RULES, {})
    assert plan.tail_present is True
    assert plan.insertion_index == len(rules) - 1


def test_c_legacy_tail_is_recognised() -> None:
    legacy_tail = {"id": "x", "who": ".*", "what": ".*", "effect": "deny"}
    rules = [*_owner_shaped(), legacy_tail]
    plan = plan_default_rule_additions(rules, STARTER_RULES, {})
    assert plan.tail_present is True
    assert plan.insertion_index == len(rules) - 1


def test_tail_not_last_inserts_before_the_first_tail() -> None:
    rules = [
        dict(_TAIL),
        {"what": "kolsch/tools/bash", "pattern": "*", "effect": "allow"},
    ]
    plan = plan_default_rule_additions(rules, STARTER_RULES, {})
    assert plan.insertion_index == 0


def test_d_ledger_decision_is_never_reoffered() -> None:
    for decision in ("installed", "added", "declined", "present"):
        plan = plan_default_rule_additions(
            _owner_shaped(), STARTER_RULES, {"jev_decide": decision}
        )
        assert plan.proposals == (), decision


def test_e_user_rule_for_the_target_counts_as_present() -> None:
    rules = [
        *_owner_shaped(),
        {"what": "kolsch/tools/jev_decide", "pattern": "x*", "effect": "deny"},
    ]
    plan = plan_default_rule_additions(rules, STARTER_RULES, {})
    assert plan.proposals == ()
    assert "jev_decide" in plan.present


def test_from_scoped_and_locked_rules_count_as_present() -> None:
    scoped = {
        "what": "kolsch/tools/jev_decide",
        "pattern": "*",
        "effect": "allow",
        "from": "kolsch/workflows/triage-tickets-jev",
    }
    plan = plan_default_rule_additions([*_owner_shaped(), scoped], STARTER_RULES, {})
    assert "jev_decide" in plan.present
    locked = [{"what": "kolsch/tools/jev_decide", "pattern": "*", "effect": "deny"}]
    plan = plan_default_rule_additions(
        _owner_shaped(), STARTER_RULES, {}, locked_rules=locked
    )
    assert "jev_decide" in plan.present


def test_legacy_rule_with_same_what_does_not_count() -> None:
    legacy = {
        "id": "l",
        "who": ".*",
        "what": "kolsch/tools/jev_decide",
        "effect": "deny",
    }
    plan = plan_default_rule_additions([*_owner_shaped(), legacy], STARTER_RULES, {})
    assert _keys(plan.proposals) == ["jev_decide"]


def test_f_missing_allow_default_is_report_only() -> None:
    plan = plan_default_rule_additions(
        _owner_shaped(drop=("workflow_status",)), STARTER_RULES, {"jev_decide": "added"}
    )
    assert plan.proposals == ()
    assert _keys(plan.report_only) == ["workflow_status"]


def test_tail_and_allow_are_never_proposed() -> None:
    plan = plan_default_rule_additions([], STARTER_RULES, {})
    assert {e.effect for e in plan.proposals} <= {"prompt", "deny"}
    assert not any(e.tail for e in plan.proposals + plan.report_only)
    assert "agent_execute" in _keys(plan.report_only)


def test_unknown_tool_is_unavailable_not_proposed() -> None:
    plan = plan_default_rule_additions(
        _owner_shaped(),
        STARTER_RULES,
        {},
        is_known_ref=lambda ref: ref != "kolsch/tools/jev_decide",
    )
    assert plan.proposals == ()
    assert _keys(plan.unavailable) == ["jev_decide"]


def test_g_apply_preserves_every_existing_rule_and_order() -> None:
    rules = [*_owner_shaped(drop=("jev_decide", "bash")), dict(_TAIL)]
    before = copy.deepcopy(rules)
    plan = plan_default_rule_additions(rules, STARTER_RULES, {})
    after = apply_default_rule_additions(rules, plan.proposals, plan.insertion_index)
    assert rules == before  # input not mutated
    added = [r for r in after if r not in before]
    assert added == [e.as_rule() for e in plan.proposals]
    assert [r for r in after if r in before] == before
    assert Counter(map(repr, after)) - Counter(map(repr, added)) == Counter(
        map(repr, before)
    )
    assert after[-1] == _TAIL
    assert {r["effect"] for r in added} <= {"prompt", "deny"}
