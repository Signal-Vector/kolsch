"""Component tests for the Phase 2.0 authz surface — TC-TOOL-12..15.

Spec: docs/process/phase2.0-tools-specs.md Part 2 (the prompt
effect) + Part 4 (live editing). Module API Reference Part 11.8
covers the runtime API the CLI calls into.

These exercise the new authz surface directly via
``RuleBasedAuthorizationProvider`` (no tools-subsystem mediation
required — the live-edit semantics and the prompt-callback shape
are properties of the authz provider itself).
"""

from __future__ import annotations

import os

import pytest

from errors import (
    AuthzProtectedRule,
    AuthzRuleNotFound,
    ConfigInvalid,
)
from providers import (
    DEFAULT_DENY_RULE_ID,
    KOLSCH_TEST_BUILD_ENV_VAR,
    AllowAllTestAuthorizationProvider,
    AuthzEffect,
    Rule,
    RuleBasedAuthorizationProvider,
    derive_rule_id,
    make_authorization_provider,
)


def _default_rules() -> RuleBasedAuthorizationProvider:
    """Workspace-only default set, faithful to Phase 1's authz
    semantics (``who`` = principal regex, ``what`` = action regex).

    *(Note for the spec reviewer: Phase 2.0 patch §3.1 shows the
    sample rules with ``who: "tool.read_file"``, which inverts the
    Phase 1 ``who/what`` convention from
    ``docs/explanation/secrets-sandbox-authz-contract.md`` §3.2.1.
    The implementation honours the existing convention; the
    patch's example is treated as a typo and tests use the correct
    semantics.)*"""
    return RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"who": ".*", "what": r"tool\.read_file", "effect": "allow"},
                {"who": ".*", "what": r"tool\.list_files", "effect": "allow"},
                {"who": ".*", "what": r"tool\.write_file", "effect": "prompt"},
                {"who": ".*", "what": r"tool\.bash", "effect": "prompt"},
                {"who": ".*", "what": ".*", "effect": "deny"},
            ]
        }
    )


# ===========================================================================
# TC-TOOL-12 — prompt effect: callback returns ALLOW; tool would run
# ===========================================================================


async def test_tc_tool_12_prompt_effect_callback_returns_allow() -> None:
    """TC-TOOL-12 (callback ALLOW): a prompt-rule match with a
    callback that returns ``ALLOW`` resolves to
    ``allowed=True``. No rule is persisted. The next call still hits
    the same prompt rule (no implicit always-allow).
    """
    provider = _default_rules()
    invoked: list[tuple[str | None, str, str | None]] = []

    async def callback(
        *, rule_id: str | None, action: str, target: str | None
    ) -> AuthzEffect:
        invoked.append((rule_id, action, target))
        return AuthzEffect.ALLOW

    result = await provider.allow("agent.helper", "tool.write_file", callback=callback)
    assert result.allowed is True
    assert result.effect is AuthzEffect.ALLOW
    assert len(invoked) == 1

    # No new rule was persisted; the same prompt rule is still matched
    # on the next call (callback fires again).
    result2 = await provider.allow("agent.helper", "tool.write_file", callback=callback)
    assert result2.effect is AuthzEffect.ALLOW
    assert len(invoked) == 2


async def test_tc_tool_12_prompt_effect_callback_persists_rule() -> None:
    """TC-TOOL-12 (callback ALLOW + persist): the callback returns a
    tuple ``(ALLOW, rule)`` — the rule is appended in-memory before
    the call returns; subsequent matching calls hit the new rule
    WITHOUT invoking the callback again.
    """
    provider = _default_rules()
    invocations = 0

    async def callback(
        *, rule_id: str | None, action: str, target: str | None
    ) -> tuple[AuthzEffect, Rule]:
        nonlocal invocations
        invocations += 1
        _ = rule_id, action, target
        return (
            AuthzEffect.ALLOW,
            Rule(
                who=".*",
                what=r"tool\.write_file",
                effect="allow",
            ),
        )

    result = await provider.allow("agent.helper", "tool.write_file", callback=callback)
    assert result.allowed is True
    assert invocations == 1

    # Next call: hits the new ALLOW rule first, callback NOT invoked.
    result2 = await provider.allow("agent.helper", "tool.write_file", callback=callback)
    assert result2.allowed is True
    assert result2.effect is AuthzEffect.ALLOW
    assert invocations == 1


async def test_prompt_without_callback_returns_prompt_effect() -> None:
    """No callback supplied (``kolsch run``, server, tests, golden) —
    the result carries ``effect=PROMPT, allowed=False``. The
    dispatcher uses ``effect=PROMPT`` to emit
    ``authz.prompt_unattended`` rather than the plain ``authz.denied``
    that an explicit deny rule would produce.
    """
    provider = _default_rules()
    result = await provider.allow("agent.helper", "tool.write_file")
    assert result.effect is AuthzEffect.PROMPT
    assert result.allowed is False
    assert result.matched_rule is not None
    assert result.matched_rule.effect == "prompt"


# ===========================================================================
# TC-TOOL-13 — list_rules ordered with stable ids
# ===========================================================================


async def test_tc_tool_13_list_rules_ordered_with_stable_ids() -> None:
    """TC-TOOL-13: list_rules returns rules in evaluation order, each
    with its auto-derived stable id; the default-deny tail keeps its
    fixed ``rl_default_deny`` id; load+reload produces byte-identical
    id assignment.
    """
    provider_first = _default_rules()
    rules_first = await provider_first.list_rules()
    assert [r.effect for r in rules_first] == [
        "allow",
        "allow",
        "prompt",
        "prompt",
        "deny",
    ]
    assert rules_first[-1].id == DEFAULT_DENY_RULE_ID
    assert all(r.id.startswith("rl_") and len(r.id) == 11 for r in rules_first[:-1])

    # Same input → same ids (deterministic per Module API Reference
    # Part 11.8.3).
    provider_second = _default_rules()
    rules_second = await provider_second.list_rules()
    assert [r.id for r in rules_first] == [r.id for r in rules_second]


async def test_derive_rule_id_is_pure_and_deterministic() -> None:
    """The id-derivation helper is itself a pure function — same
    inputs always produce the same id; different inputs produce
    different ids."""
    a = derive_rule_id(".*", r"tool\.bash", "prompt")
    b = derive_rule_id(".*", r"tool\.bash", "prompt")
    c = derive_rule_id(".*", r"tool\.bash", "allow")
    assert a == b
    assert a != c
    assert derive_rule_id(".*", ".*", "deny") == DEFAULT_DENY_RULE_ID


# ===========================================================================
# TC-TOOL-14 — remove rl_default_deny is rejected
# ===========================================================================


async def test_tc_tool_14_remove_default_deny_is_rejected() -> None:
    """TC-TOOL-14: kolsch permissions remove rl_default_deny →
    AuthzProtectedRule (Fatal). The rule list is unchanged.
    """
    provider = _default_rules()
    before = await provider.list_rules()
    with pytest.raises(AuthzProtectedRule):
        await provider.remove_rule(DEFAULT_DENY_RULE_ID)
    after = await provider.list_rules()
    assert [r.id for r in before] == [r.id for r in after]


async def test_remove_unknown_rule_raises_rule_not_found() -> None:
    """Symmetric coverage: removing an unknown id raises
    AuthzRuleNotFound carrying the available_ids for the user."""
    provider = _default_rules()
    with pytest.raises(AuthzRuleNotFound) as excinfo:
        await provider.remove_rule("rl_nonexistent")
    assert excinfo.value.detail["id"] == "rl_nonexistent"
    assert DEFAULT_DENY_RULE_ID in excinfo.value.detail["available_ids"]


# ===========================================================================
# TC-TOOL-15 — live edit during a running provider; same exchange
# ===========================================================================


async def test_tc_tool_15_live_edit_applies_within_same_exchange() -> None:
    """TC-TOOL-15: the live-edit guarantee — a rule added during an
    exchange is visible to the next ``allow`` call without restart.

    Sequence:

    1. ``allow`` matches the prompt rule (no callback) → PROMPT.
    2. ``add_rule`` inserts an ALLOW rule *before* the prompt rule.
    3. ``allow`` again — hits the new rule first; resolves to ALLOW.

    This is the load-bearing "rule changes apply within the same
    exchange" guarantee from Phase 2.0 patch §0.
    """
    provider = _default_rules()

    first = await provider.allow("agent.helper", "tool.write_file")
    assert first.effect is AuthzEffect.PROMPT

    new_rule_id = derive_rule_id(".*", r"tool\.write_file", "allow")
    prompt_rule = (await provider.list_rules())[2]
    assert prompt_rule.effect == "prompt"
    await provider.add_rule(
        Rule(who=".*", what=r"tool\.write_file", effect="allow"),
        before=prompt_rule.id,
    )

    second = await provider.allow("agent.helper", "tool.write_file")
    assert second.allowed is True
    assert second.effect is AuthzEffect.ALLOW
    assert second.rule_id == new_rule_id


async def test_add_rule_default_position_lands_before_tail() -> None:
    """The default ``add_rule`` insertion point (no before/after/first)
    is just before the default-deny tail — the most useful position
    for an additive allow/deny rule."""
    provider = _default_rules()
    await provider.add_rule(
        Rule(who=r"agent\.special", what=".*", effect="allow"),
    )
    rules = await provider.list_rules()
    assert rules[-1].id == DEFAULT_DENY_RULE_ID
    assert rules[-2].who == r"agent\.special"


# ===========================================================================
# allow_all_test variant — TOOL-R10 / spec §3.7.5
# ===========================================================================


def test_allow_all_test_variant_refuses_to_load_without_env_var() -> None:
    """Production builds (env var unset) refuse to construct the
    variant — ConfigInvalid Fatal at bootstrap. Pinned per
    AGENTS.md hard rule per TOOL-R10."""
    # Make sure the env var is unset for this test.
    saved = os.environ.pop(KOLSCH_TEST_BUILD_ENV_VAR, None)
    try:
        with pytest.raises(ConfigInvalid):
            make_authorization_provider(
                config={"variant": "allow_all_test", "config": {}}
            )
    finally:
        if saved is not None:
            os.environ[KOLSCH_TEST_BUILD_ENV_VAR] = saved


async def test_allow_all_test_variant_loads_with_env_var_set() -> None:
    """Setting ``KOLSCH_TEST_BUILD=1`` lets the variant load and
    return ALLOW for everything — the test-only convenience for
    component tests that don't want to declare a permissive rule
    set."""
    saved = os.environ.get(KOLSCH_TEST_BUILD_ENV_VAR)
    os.environ[KOLSCH_TEST_BUILD_ENV_VAR] = "1"
    try:
        provider = make_authorization_provider(
            config={"variant": "allow_all_test", "config": {}}
        )
        result = await provider.allow("anyone", "anything.at.all")
        assert result.allowed is True
        assert result.effect is AuthzEffect.ALLOW
        assert isinstance(provider, AllowAllTestAuthorizationProvider)
    finally:
        if saved is None:
            os.environ.pop(KOLSCH_TEST_BUILD_ENV_VAR, None)
        else:
            os.environ[KOLSCH_TEST_BUILD_ENV_VAR] = saved
