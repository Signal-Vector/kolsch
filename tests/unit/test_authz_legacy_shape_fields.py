"""bug-159 — legacy-shape authz rules cannot carry `from` / `pattern`.

The legacy `{id, who, what, effect}` regex shape has no caller-chain or
target filter. Before bug-159 every compile seam silently ignored a
`from:` / `pattern:` on such a rule, so a rule the author scoped to one
agent (or one target glob) was enforced for every caller and target.
These tests pin that every seam now refuses the combination:

- platform rule load (`_compile_legacy`) → `AuthzMalformedRule`;
- `providers.authz.Rule` construction → Pydantic `ValidationError`;
- `schemas.config.PermissionRule` validation → Pydantic `ValidationError`;
- workflow `policy:` parse → `WorkflowValidationError`;
- `_compile_rule_record` (`add_rule`, per-call layer compile) for a
  `model_construct` bypass → `AuthzMalformedRule`, rule list unchanged.

Every probe uses an explicit `caller_chain`, so the assertions hold with
or without tool-path chain wiring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from errors import AuthzMalformedRule
from providers.authz import (
    AuthzEffect,
    PermissionLayer,
    Rule,
    RuleBasedAuthorizationProvider,
)
from schemas.config import PermissionRule
from workflows.errors import WorkflowValidationError
from workflows.parser import load_workflow

if TYPE_CHECKING:
    from pathlib import Path

A = "kolsch/agents/a"
B = "kolsch/agents/b"
_CHAINS: tuple[tuple[str, ...], ...] = ((A,), (B,), ())
_TAIL: dict[str, str] = {"what": "*", "pattern": "*", "effect": "deny"}


def _provider(rules: list[Any]) -> RuleBasedAuthorizationProvider:
    return RuleBasedAuthorizationProvider(config={"rules": rules})


async def _effects(
    provider: RuleBasedAuthorizationProvider,
    action: str,
    *,
    target: str | None = None,
    layers: tuple[PermissionLayer, ...] = (),
) -> list[AuthzEffect]:
    return [
        (
            await provider.allow(
                "user.local",
                action,
                target=target,
                layers=layers,
                caller_chain=chain,
            )
        ).effect
        for chain in _CHAINS
    ]


def _assert_hint(text: str, field: str, *, setup: bool = False) -> None:
    """The refusal names the field and the fix; `kolsch setup` only when it works."""
    assert field in text
    assert "legacy" in text
    assert "by hand to the canonical shape" in text
    assert "from: <bundle>/agents/<name>" in text
    assert ("kolsch setup" in text) is setup


# ---------------------------------------------------------------------------
# AC 1 / AC 2 — platform rule load
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("from_key", ["from", "from_"])
@pytest.mark.parametrize("from_value", [A, "not-a-ref"])
def test_platform_load_rejects_legacy_from(from_key: str, from_value: str) -> None:
    rule = {"who": ".*", "what": r"agent\.execute\..*", from_key: from_value}
    rule["effect"] = "allow"
    with pytest.raises(AuthzMalformedRule) as excinfo:
        _provider([rule, _TAIL])
    assert excinfo.value.detail["rule_index"] == 0
    # The migrator converts a valid `from:` ref; a garbage one it refuses.
    _assert_hint(excinfo.value.detail["error"], "`from:`", setup=from_value == A)


@pytest.mark.parametrize(
    "rule",
    [
        {"who": "^user$", "what": ".*", "from": A, "effect": "allow"},
        {"who": ".*", "what": "^[a-z]+$", "from": A, "effect": "allow"},
        {"who": ".*", "what": ".*", "from": "kolsch/tools/x", "effect": "allow"},
        {"who": ".*", "what": ".*", "from": A, "pattern": "a*", "effect": "allow"},
    ],
    ids=["non_star_who", "unrecognised_what", "bad_from_type", "with_pattern"],
)
def test_platform_load_hint_omits_setup_when_migrator_refuses(
    rule: dict[str, str],
) -> None:
    with pytest.raises(AuthzMalformedRule) as excinfo:
        _provider([rule, _TAIL])
    assert "kolsch setup" not in excinfo.value.detail["error"]
    assert "by hand" in excinfo.value.detail["error"]


async def test_null_from_does_not_hide_from_underscore_on_legacy() -> None:
    """QA: `from: null` + `from_: <ref>` used to load unscoped."""
    rule = {"who": ".*", "what": r"tool\.bash", "from": None, "from_": A}
    rule["effect"] = "allow"
    with pytest.raises(AuthzMalformedRule):
        _provider([rule, _TAIL])


async def test_null_from_does_not_hide_from_underscore_on_canonical() -> None:
    provider = _provider(
        [
            {
                "what": "agent.execute.*",
                "from": None,
                "from_": A,
                "effect": "allow",
            },
            _TAIL,
        ]
    )
    assert (await provider.list_rules())[0].from_ == A
    assert await _effects(provider, "agent.execute.ag_x") == [
        AuthzEffect.ALLOW,
        AuthzEffect.DENY,
        AuthzEffect.DENY,
    ]


@pytest.mark.parametrize("who", [None, ".*"], ids=["canonical", "legacy"])
def test_conflicting_from_and_from_underscore_refused(who: str | None) -> None:
    rule: dict[str, Any] = {"what": "*", "from": A, "from_": "kolsch/agents/b"}
    rule["effect"] = "allow"
    if who is not None:
        rule["who"] = who
    with pytest.raises(AuthzMalformedRule) as excinfo:
        _provider([rule, _TAIL])
    assert "both" in excinfo.value.detail["error"]


def test_platform_load_rejects_legacy_non_star_pattern() -> None:
    rule = {"who": ".*", "what": "x", "pattern": "safe*", "effect": "allow"}
    with pytest.raises(AuthzMalformedRule) as excinfo:
        _provider([{"what": "agent.execute.*", "effect": "allow"}, rule, _TAIL])
    assert excinfo.value.detail["rule_index"] == 1
    _assert_hint(excinfo.value.detail["error"], "`pattern: 'safe*'`")


def test_platform_load_names_both_fields() -> None:
    rule = {"who": ".*", "what": "x", "pattern": "a*", "from": A, "effect": "deny"}
    with pytest.raises(AuthzMalformedRule) as excinfo:
        _provider([rule])
    error = excinfo.value.detail["error"]
    assert "`from:`" in error
    assert "`pattern: 'a*'`" in error


async def test_platform_load_accepts_legacy_star_pattern_and_null_from() -> None:
    provider = _provider(
        [
            {
                "who": ".*",
                "what": r"tool\.bash",
                "pattern": "*",
                "from": None,
                "effect": "allow",
            },
            _TAIL,
        ]
    )
    rules = await provider.list_rules()
    assert rules[0].who == ".*"
    assert rules[0].from_ is None
    assert rules[0].pattern == "*"


# ---------------------------------------------------------------------------
# AC 6 — the deep-dive probes, re-run as assertions
# ---------------------------------------------------------------------------


async def test_probe_canonical_from_scopes_to_caller() -> None:
    """The canonical form of the probe rule enforces the scope."""
    provider = _provider(
        [
            {
                "what": "agent.execute.*",
                "pattern": "*",
                "from": A,
                "effect": "allow",
            },
            _TAIL,
        ]
    )
    assert await _effects(provider, "agent.execute.ag_x") == [
        AuthzEffect.ALLOW,
        AuthzEffect.DENY,
        AuthzEffect.DENY,
    ]


@pytest.mark.parametrize(
    ("what", "action", "target"),
    [
        (r"agent\.execute\..*", "agent.execute.ag_x", None),
        (r"tool\.bash", "tool.bash", "rm -rf /"),
    ],
)
async def test_probe_legacy_from_no_longer_allows_every_caller(
    what: str, action: str, target: str | None
) -> None:
    """Pre-fix: ALLOW for `(A,)`, `(B,)` and `()`. Now refused at load."""
    try:
        provider = _provider(
            [{"who": ".*", "what": what, "from": A, "effect": "allow"}, _TAIL]
        )
    except AuthzMalformedRule:
        return
    effects = await _effects(provider, action, target=target)
    pytest.fail(f"scoped legacy rule loaded; chains {_CHAINS} -> {effects}")


async def test_probe_legacy_pattern_no_longer_matches_every_target() -> None:
    """Pre-fix: `pattern: safe*` allowed target `danger`. Now refused."""
    try:
        provider = _provider(
            [{"who": ".*", "what": "x", "pattern": "safe*", "effect": "allow"}, _TAIL]
        )
    except AuthzMalformedRule:
        return
    result = await provider.allow("user.local", "x", target="danger")
    pytest.fail(f"legacy `pattern: safe*` loaded; target 'danger' -> {result.effect}")


# ---------------------------------------------------------------------------
# AC 4 — `Rule` construction (D1)
# ---------------------------------------------------------------------------


def test_rule_rejects_legacy_from() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Rule(who=".*", what=r"tool\.bash", from_=A, effect="allow")
    _assert_hint(str(excinfo.value), "`from:`")


def test_rule_rejects_legacy_from_via_alias() -> None:
    with pytest.raises(ValidationError):
        Rule.model_validate(
            {"who": ".*", "what": r"tool\.bash", "from": A, "effect": "allow"}
        )


def test_rule_rejects_legacy_non_star_pattern() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Rule(who=".*", what="x", pattern="safe*", effect="allow")
    assert "`pattern: 'safe*'`" in str(excinfo.value)


def test_rule_accepts_unscoped_legacy_and_scoped_canonical() -> None:
    legacy = Rule(who=".*", what=r"tool\.bash", effect="prompt", pattern="*")
    assert legacy.is_legacy
    canonical = Rule(what="kolsch/tools/bash", pattern="ls *", from_=A, effect="allow")
    assert canonical.from_ == A


def test_workflow_policy_rejects_legacy_from(tmp_path: Path) -> None:
    path = tmp_path / "wf.yaml"
    path.write_text(
        "policy:\n"
        "  - who: '.*'\n"
        "    what: 'tool\\.bash'\n"
        f"    from: {A}\n"
        "    effect: allow\n",
        encoding="utf-8",
    )
    with pytest.raises(WorkflowValidationError) as excinfo:
        load_workflow(path)
    assert "`from:`" in str(excinfo.value)


# ---------------------------------------------------------------------------
# AC 3 — `PermissionRule` (agent / bundle `permissions:`)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "field"),
    [({"from": A}, "`from:`"), ({"pattern": "safe*"}, "`pattern: 'safe*'`")],
)
def test_permission_rule_rejects_scoped_legacy(
    extra: dict[str, str], field: str
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        PermissionRule.model_validate(
            {"who": ".*", "what": "x", "effect": "allow"} | extra
        )
    _assert_hint(str(excinfo.value), field)


def test_permission_rule_accepts_unscoped_legacy_and_scoped_canonical() -> None:
    assert (
        PermissionRule.model_validate(
            {"who": ".*", "what": "x", "pattern": "*", "effect": "allow"}
        ).who
        == ".*"
    )
    assert (
        PermissionRule.model_validate(
            {"what": "kolsch/tools/bash", "from": A, "effect": "allow"}
        ).from_
        == A
    )


def test_permission_rule_and_rule_share_the_message() -> None:
    raw = {"who": ".*", "what": "x", "from": A, "effect": "allow"}
    with pytest.raises(ValidationError) as rule_exc:
        Rule.model_validate(raw)
    with pytest.raises(ValidationError) as perm_exc:
        PermissionRule.model_validate(raw)
    assert rule_exc.value.errors()[0]["msg"] == perm_exc.value.errors()[0]["msg"]


# ---------------------------------------------------------------------------
# AC 5 — `_compile_rule_record` defense in depth
# ---------------------------------------------------------------------------


def _bypassed(**fields: Any) -> Rule:
    """Build a legacy `Rule` that skips validation (the D1 bypass)."""
    base: dict[str, Any] = {
        "who": ".*",
        "what": r"tool\.bash",
        "pattern": "*",
        "effect": "allow",
        "from_": None,
        "id": "",
        "source_file": None,
        "source_line": None,
    }
    return Rule.model_construct(**(base | fields))


@pytest.mark.parametrize(
    "fields", [{"from_": A}, {"pattern": "safe*"}], ids=["from", "pattern"]
)
async def test_add_rule_rejects_bypassed_legacy_rule(fields: dict[str, Any]) -> None:
    provider = _provider([{"what": "kolsch/tools/ls", "effect": "allow"}, _TAIL])
    before = await provider.list_rules()
    with pytest.raises(AuthzMalformedRule) as excinfo:
        await provider.add_rule(_bypassed(**fields))
    assert excinfo.value.detail["rule_index"] == -1
    assert await provider.list_rules() == before


async def test_add_rule_rejects_model_copy_bypass() -> None:
    provider = _provider([_TAIL])
    sneaky = Rule(who=".*", what=r"tool\.bash", effect="allow").model_copy(
        update={"from_": A}
    )
    with pytest.raises(AuthzMalformedRule):
        await provider.add_rule(sneaky)
    assert len(await provider.list_rules()) == 1


async def test_layer_compile_rejects_bypassed_legacy_rule() -> None:
    """Pre-fix probe: the agent layer's scoped legacy allow fired for `(B,)`."""
    provider = _provider([_TAIL])
    layer = PermissionLayer(name="agent", rules=(_bypassed(from_=A),))
    with pytest.raises(AuthzMalformedRule):
        await provider.allow(
            "user.local", "tool.bash", layers=(layer,), caller_chain=(B,)
        )


# ---------------------------------------------------------------------------
# AC 6 — no legacy compiled rule holds an ignored field
# ---------------------------------------------------------------------------


async def test_no_compiled_legacy_rule_holds_from_or_pattern() -> None:
    provider = _provider(
        [
            {"who": ".*", "what": r"tool\.bash", "effect": "prompt"},
            {"id": "rl_custom", "who": ".*", "what": "x", "effect": "allow"},
            {
                "what": "kolsch/tools/bash",
                "pattern": "ls *",
                "from": A,
                "effect": "allow",
            },
            _TAIL,
        ]
    )
    await provider.add_rule(Rule(who=".*", what="y", effect="deny"))
    for compiled in provider._rules:
        if compiled.is_legacy:
            assert compiled.raw.from_ is None
            assert compiled.raw.pattern == "*"


# ---------------------------------------------------------------------------
# AC 7 — unscoped legacy rules unchanged
# ---------------------------------------------------------------------------


async def test_unscoped_legacy_rules_load_and_match_as_before() -> None:
    provider = _provider(
        [
            {
                "id": "rl_281a0511",
                "who": ".*",
                "what": r"^agent\.execute\.ag_butler$",
                "effect": "allow",
            },
            {"who": ".*", "what": r"tool\.bash", "effect": "allow"},
            {"who": ".*", "what": ".*", "effect": "deny"},
        ]
    )
    rules = await provider.list_rules()
    assert [r.id for r in rules] == ["rl_281a0511", "rl_de710548", "rl_default_deny"]
    assert (
        await _effects(provider, "agent.execute.ag_butler") == [AuthzEffect.ALLOW] * 3
    )
    assert await _effects(provider, "tool.bash", target="ls") == [AuthzEffect.ALLOW] * 3
    assert await _effects(provider, "agent.execute.ag_other") == [AuthzEffect.DENY] * 3
