"""feat-160 — locked platform rules in the rule-based authz provider.

`authorization.config.locked_rules` is a deny-only list evaluated
before every caller layer and before the platform `rules`. These tests
pin the stage order, the additive guarantee when the list is absent,
the strict load-time validation, the `rl_locked_` ids and the
live-edit API.
"""

from __future__ import annotations

from typing import Any

import pytest

from assets.catalog import AssetCatalog, AssetEntry
from assets.manifest import BundleManifest
from errors import (
    AuthzMalformedRule,
    AuthzPatternInvalid,
    AuthzRuleInvalid,
    AuthzRuleNotFound,
    AuthzToolRefUnknown,
)
from providers import (
    DEFAULT_DENY_RULE_ID,
    LOCKED_RULE_ID_PREFIX,
    AuthorizationResult,
    AuthzEffect,
    PermissionLayer,
    Rule,
    RuleBasedAuthorizationProvider,
    derive_locked_rule_id,
    derive_rule_id,
)

_BASH = "kolsch/tools/bash"
_READ = "kolsch/tools/read_file"
_LOCKED_BASH = {"what": _BASH, "pattern": "*", "effect": "deny"}
_TAIL = {"what": "*", "pattern": "*", "effect": "deny"}
_ALLOW_BASH = (Rule(what=_BASH, pattern="*", effect="allow"),)
_PLATFORM_RULES: list[dict[str, Any]] = [
    {"what": _BASH, "pattern": "*", "effect": "allow"},
    {"what": _READ, "pattern": "/ws/*", "effect": "allow"},
    {"what": "kolsch/tools/write_file", "pattern": "*", "effect": "prompt"},
    {"what": "agent.execute.*", "pattern": "*", "effect": "allow"},
    _TAIL,
]


def _provider(
    *,
    locked: object = None,
    rules: list[dict[str, Any]] | None = None,
    include_locked_key: bool = True,
    **kwargs: Any,
) -> RuleBasedAuthorizationProvider:
    config: dict[str, Any] = {"rules": list(rules if rules is not None else [])}
    if include_locked_key:
        config["locked_rules"] = locked
    return RuleBasedAuthorizationProvider(config=config, **kwargs)


def _catalog(*tool_refs: tuple[str, str]) -> AssetCatalog:
    assets: dict[str, AssetEntry] = {}
    bundles: dict[str, BundleManifest] = {}
    for bundle, name in tool_refs:
        ref = f"{bundle}/tools/{name}"
        assets[ref] = AssetEntry(
            ref=ref,
            asset_type="tool",
            bundle=bundle,
            path=f"bundles/{bundle}/tools/{name}.py",
        )
        bundles.setdefault(
            bundle,
            BundleManifest(
                name=bundle,
                version="0.1.0",
                description=f"{bundle} bundle (test fixture)",
                path=f"/tmp/bundles/{bundle}",
            ),
        )
    return AssetCatalog(bundles=bundles, assets=assets)


def _assert_locked_deny(result: AuthorizationResult) -> None:
    assert result.allowed is False
    assert result.effect is AuthzEffect.DENY
    assert result.rule_id is not None
    assert result.rule_id.startswith(LOCKED_RULE_ID_PREFIX)
    assert result.reason.endswith("(who_chain=locked)")


# ---------------------------------------------------------------------------
# AC 2 — absolute: a locked deny beats every layer and the platform
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layer_name", ["workflow", "agent", "bundle"])
async def test_locked_deny_beats_layer_allow(layer_name: str) -> None:
    authz = _provider(locked=[_LOCKED_BASH], rules=_PLATFORM_RULES)
    result = await authz.allow(
        "user.local",
        _BASH,
        target="rm -rf /tmp/x",
        layers=(PermissionLayer(name=layer_name, rules=_ALLOW_BASH),),
    )
    _assert_locked_deny(result)
    assert result.matched_rule is not None
    assert result.matched_rule.what == _BASH
    assert result.matched_rule.pattern == "*"


async def test_locked_deny_beats_all_layers_together_and_platform_allow() -> None:
    authz = _provider(locked=[_LOCKED_BASH], rules=_PLATFORM_RULES)
    layers = tuple(
        PermissionLayer(name=name, rules=_ALLOW_BASH)
        for name in ("workflow", "agent", "bundle")
    )
    result = await authz.allow(
        "user.local", _BASH, target="ls", layers=layers, caller_chain=("a/agents/b",)
    )
    _assert_locked_deny(result)
    # Platform allow alone is beaten too.
    _assert_locked_deny(await authz.allow("user.local", _BASH, target="ls"))


async def test_locked_pattern_deny_only_hits_matching_targets() -> None:
    authz = _provider(
        locked=[{"what": _READ, "pattern": "/ws/secret/*", "effect": "deny"}],
        rules=_PLATFORM_RULES,
    )
    _assert_locked_deny(
        await authz.allow("user.local", _READ, target="/ws/secret/key.pem")
    )
    allowed = await authz.allow("user.local", _READ, target="/ws/notes.md")
    assert allowed.allowed is True
    assert allowed.reason.endswith("(who_chain=platform)")


async def test_first_locked_match_wins() -> None:
    first = {"what": _BASH, "pattern": "rm *", "effect": "deny"}
    authz = _provider(locked=[first, _LOCKED_BASH], rules=_PLATFORM_RULES)
    result = await authz.allow("user.local", _BASH, target="rm -rf /")
    assert result.rule_id == derive_locked_rule_id(_BASH, "rm *", "deny")


async def test_locked_deny_never_invokes_prompt_callback() -> None:
    calls: list[str | None] = []

    async def callback(
        *,
        rule_id: str | None,
        action: str,
        target: str | None,
        caller_chain: tuple[str, ...] = (),
        execution_context: object = None,
    ) -> AuthzEffect:
        calls.append(rule_id)
        return AuthzEffect.ALLOW

    # The platform list is empty, so without the lock this action
    # would reach the no-rule callback; prompt rules in a layer would
    # reach `_resolve_prompt`.
    authz = _provider(locked=[_LOCKED_BASH])
    authz.register_prompt_callback(callback)
    prompt_layer = PermissionLayer(
        name="agent", rules=(Rule(what=_BASH, pattern="*", effect="prompt"),)
    )
    _assert_locked_deny(
        await authz.allow("user.local", _BASH, target="ls", layers=(prompt_layer,))
    )
    _assert_locked_deny(
        await authz.allow("user.local", _BASH, target="ls", callback=callback)
    )
    assert calls == []


# ---------------------------------------------------------------------------
# AC 1 / AC 3 — additive and inert
# ---------------------------------------------------------------------------


_MATRIX: list[tuple[str, str | None, tuple[PermissionLayer, ...]]] = [
    (_BASH, "rm -rf /tmp/x", ()),
    (_BASH, "ls", (PermissionLayer(name="workflow", rules=_ALLOW_BASH),)),
    (
        _BASH,
        "ls",
        (
            PermissionLayer(
                name="agent", rules=(Rule(what=_BASH, pattern="*", effect="deny"),)
            ),
        ),
    ),
    (_READ, "/ws/a.md", ()),
    (_READ, "/etc/passwd", ()),
    ("kolsch/tools/write_file", "/ws/a.md", ()),
    ("agent.execute.kolsch/agents/helper", None, ()),
    ("kolsch/tools/unknown", "x", ()),
]


async def _decisions(
    authz: RuleBasedAuthorizationProvider,
) -> list[AuthorizationResult]:
    return [
        await authz.allow("user.local", action, target=target, layers=layers)
        for action, target, layers in _MATRIX
    ]


async def test_absent_empty_and_null_locked_rules_are_identical() -> None:
    absent = _provider(rules=_PLATFORM_RULES, include_locked_key=False)
    empty = _provider(locked=[], rules=_PLATFORM_RULES)
    null = _provider(locked=None, rules=_PLATFORM_RULES)
    baseline = await _decisions(absent)
    assert await _decisions(empty) == baseline
    assert await _decisions(null) == baseline
    assert [r.reason for r in baseline] == [r.reason for r in await _decisions(null)]
    assert await empty.list_rules() == await absent.list_rules()
    assert await null.list_rules() == await absent.list_rules()
    assert await absent.list_locked_rules() == []
    assert await null.list_locked_rules() == []


async def test_non_matching_locked_rule_is_inert() -> None:
    unlocked = _provider(rules=_PLATFORM_RULES, include_locked_key=False)
    # Locks a tool none of the matrix actions use.
    locked = _provider(
        locked=[{"what": "kolsch/tools/browse_url", "effect": "deny"}],
        rules=_PLATFORM_RULES,
    )
    assert await _decisions(locked) == await _decisions(unlocked)


# ---------------------------------------------------------------------------
# AC 4 — validation fails loud at load
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("locked", "needle"),
    [
        ({"what": _BASH, "effect": "deny"}, "must be a list"),
        ("nope", "must be a list"),
        (["kolsch/tools/bash"], "locked_rules[0]: rule must be a mapping"),
        (
            [{"what": _BASH, "pattern": "*", "effect": "deny", "note": "x"}],
            "locked_rules[0]: unknown key 'note'",
        ),
        (
            [{"who": ".*", "what": "bash", "effect": "deny"}],
            "locked_rules[0]: legacy",
        ),
        ([{"what": _BASH, "effect": "allow"}], "deny-only"),
        ([{"what": _BASH, "effect": "prompt"}], "deny-only"),
        (
            [{"what": _BASH, "effect": "deny", "from": "a/agents/b"}],
            "caller chain",
        ),
        (
            [{"what": _BASH, "effect": "deny", "from_": "a/agents/b"}],
            "caller chain",
        ),
        ([{"what": _BASH, "effect": "deny", "id": "rl_x"}], "`id` is not accepted"),
        ([{"what": "*", "pattern": "*", "effect": "deny"}], "default-deny tail"),
        ([{"what": "*", "effect": "deny"}], "default-deny tail"),
        ([{"pattern": "*", "effect": "deny"}], "missing required field 'what'"),
        ([{"what": _BASH}], "missing required field 'effect'"),
        ([{"what": "", "effect": "deny"}], "locked_rules[0]: `what` must be"),
        (
            [{"what": _READ, "effect": "deny"}, _LOCKED_BASH, _LOCKED_BASH],
            "locked_rules[2]: duplicate of locked_rules[1]",
        ),
    ],
)
def test_invalid_locked_rules_raise_malformed(locked: object, needle: str) -> None:
    with pytest.raises(AuthzMalformedRule) as excinfo:
        _provider(locked=locked, rules=[_TAIL])
    assert needle in str(excinfo.value)
    if isinstance(locked, list):
        assert "locked_rules[" in str(excinfo.value)


async def test_null_from_is_tolerated() -> None:
    authz = _provider(locked=[{"what": _BASH, "effect": "deny", "from": None}])
    [locked] = await authz.list_locked_rules()
    assert locked.from_ is None


def test_bad_pattern_raises_pattern_invalid_naming_locked_rules() -> None:
    with pytest.raises(AuthzPatternInvalid) as excinfo:
        _provider(
            locked=[_LOCKED_BASH, {"what": _BASH, "pattern": "^rm", "effect": "deny"}]
        )
    assert excinfo.value.where == "authz.rule_based.locked_rules[1]"
    assert str(excinfo.value).startswith("locked_rules[1]: ")
    assert excinfo.value.code == "authz.pattern_invalid"


def test_unknown_tool_ref_raises_with_catalog() -> None:
    with pytest.raises(AuthzToolRefUnknown) as excinfo:
        _provider(
            locked=[{"what": "kolsch/tools/missing", "effect": "deny"}],
            catalog=_catalog(("kolsch", "bash")),
        )
    assert excinfo.value.where == "authz.rule_based.locked_rules[0]"
    assert str(excinfo.value).startswith("locked_rules[0]: ")
    assert excinfo.value.code == "authz.tool_ref_unknown"
    # A catalogued ref is fine.
    _provider(locked=[_LOCKED_BASH], catalog=_catalog(("kolsch", "bash")))


# ---------------------------------------------------------------------------
# AC 5 — distinct ids
# ---------------------------------------------------------------------------


def test_derive_locked_rule_id_is_deterministic_and_distinct() -> None:
    a = derive_locked_rule_id(_BASH, "*", "deny")
    assert a == derive_locked_rule_id(_BASH, "*", "deny")
    assert a.startswith(LOCKED_RULE_ID_PREFIX)
    assert len(a) == len(LOCKED_RULE_ID_PREFIX) + 8
    assert a != derive_locked_rule_id(_BASH, "rm *", "deny")
    assert a != derive_rule_id(".*", f"{_BASH}|*", "deny")
    # No catch-all collapse.
    assert derive_locked_rule_id("*", "*", "deny") != DEFAULT_DENY_RULE_ID


async def test_loaded_locked_rules_carry_locked_ids_and_source() -> None:
    authz = _provider(
        locked=[_LOCKED_BASH],
        rules=[_TAIL],
        locked_source_map={0: ("/ws/platform.yaml", 12)},
        source_map={0: ("/ws/platform.yaml", 30)},
    )
    [locked] = await authz.list_locked_rules()
    assert locked.id == derive_locked_rule_id(_BASH, "*", "deny")
    assert (locked.source_file, locked.source_line) == ("/ws/platform.yaml", 12)
    [tail] = await authz.list_rules()
    assert tail.id == DEFAULT_DENY_RULE_ID
    assert tail.source_line == 30


# ---------------------------------------------------------------------------
# AC 6 — provider API
# ---------------------------------------------------------------------------


async def test_list_rules_excludes_locked_rules() -> None:
    authz = _provider(locked=[_LOCKED_BASH], rules=_PLATFORM_RULES)
    ids = [r.id for r in await authz.list_rules()]
    assert len(ids) == len(_PLATFORM_RULES)
    assert not any(i.startswith(LOCKED_RULE_ID_PREFIX) for i in ids)
    assert [r.what for r in await authz.list_locked_rules()] == [_BASH]


async def test_get_rule_and_remove_rule_cover_locked_ids() -> None:
    authz = _provider(locked=[_LOCKED_BASH], rules=_PLATFORM_RULES)
    locked_id = derive_locked_rule_id(_BASH, "*", "deny")
    assert (await authz.get_rule(locked_id)).what == _BASH
    await authz.remove_rule(locked_id)
    assert await authz.list_locked_rules() == []
    assert len(await authz.list_rules()) == len(_PLATFORM_RULES)
    with pytest.raises(AuthzRuleNotFound):
        await authz.get_rule(locked_id)
    with pytest.raises(AuthzRuleNotFound):
        await authz.remove_rule(locked_id)
    # Removed lock → the platform allow decides again.
    result = await authz.allow("user.local", _BASH, target="ls")
    assert result.allowed is True


async def test_add_locked_rule_appends_and_takes_effect() -> None:
    authz = _provider(locked=[{"what": _READ, "effect": "deny"}], rules=_PLATFORM_RULES)
    added = await authz.add_locked_rule(Rule(what=_BASH, effect="deny"))
    assert added.id == derive_locked_rule_id(_BASH, "*", "deny")
    assert [r.what for r in await authz.list_locked_rules()] == [_READ, _BASH]
    assert len(await authz.list_rules()) == len(_PLATFORM_RULES)
    _assert_locked_deny(
        await authz.allow(
            "user.local",
            _BASH,
            target="ls",
            layers=(PermissionLayer(name="agent", rules=_ALLOW_BASH),),
        )
    )
    # Passing back the derived id is accepted.
    await authz.remove_rule(added.id)
    again = await authz.add_locked_rule(added)
    assert again.id == added.id


@pytest.mark.parametrize(
    ("rule", "needle"),
    [
        (Rule(what=_BASH, effect="allow"), "deny-only"),
        (Rule(what=_BASH, effect="prompt"), "deny-only"),
        (Rule(what=_BASH, effect="deny", **{"from": "a/agents/b"}), "caller chain"),
        (Rule(who=".*", what=_BASH, effect="deny"), "legacy"),
        (Rule(what=_BASH, effect="deny", id="rl_custom"), "`id` is not accepted"),
        (Rule(what="*", pattern="*", effect="deny"), "default-deny tail"),
        (Rule(what=_READ, effect="deny"), "already exists"),
        (Rule(what="", effect="deny"), "`what` must be"),
    ],
)
async def test_add_locked_rule_rejections(rule: Rule, needle: str) -> None:
    authz = _provider(locked=[{"what": _READ, "effect": "deny"}])
    with pytest.raises(AuthzRuleInvalid) as excinfo:
        await authz.add_locked_rule(rule)
    assert needle in str(excinfo.value)
    assert len(await authz.list_locked_rules()) == 1


async def test_add_locked_rule_shares_pattern_and_catalog_checks() -> None:
    authz = _provider(locked=[], catalog=_catalog(("kolsch", "bash")))
    with pytest.raises(AuthzPatternInvalid):
        await authz.add_locked_rule(Rule(what=_BASH, pattern="(rm)", effect="deny"))
    with pytest.raises(AuthzToolRefUnknown):
        await authz.add_locked_rule(Rule(what="kolsch/tools/missing", effect="deny"))
    assert await authz.list_locked_rules() == []


# ---------------------------------------------------------------------------
# B1 — a non-3-segment `what` would load and never fire; rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("with_catalog", [False, True])
@pytest.mark.parametrize(
    ("what", "needle"),
    [
        ("kolsch/bash", "did you mean 'kolsch/tools/bash'?"),
        ("kolsch/*", "must be a 3-segment ref"),
        ("a/tools/b/c", "must be a 3-segment ref"),
        ("kolsch//bash", "must be a 3-segment ref"),
    ],
)
def test_non_three_segment_what_rejected(
    what: str, needle: str, *, with_catalog: bool
) -> None:
    catalog = _catalog(("kolsch", "bash")) if with_catalog else None
    with pytest.raises(AuthzMalformedRule) as excinfo:
        _provider(locked=[{"what": what, "effect": "deny"}], catalog=catalog)
    message = str(excinfo.value)
    assert "locked_rules[0]: " in message
    assert needle in message


async def test_two_segment_what_rejected_by_add_locked_rule() -> None:
    authz = _provider(locked=[], catalog=_catalog(("kolsch", "bash")))
    with pytest.raises(AuthzRuleInvalid) as excinfo:
        await authz.add_locked_rule(Rule(what="kolsch/bash", effect="deny"))
    assert "did you mean 'kolsch/tools/bash'?" in str(excinfo.value)
    assert await authz.list_locked_rules() == []


async def test_corrected_three_segment_lock_beats_agent_allow() -> None:
    # The 2-seg spelling is refused; the canonical one the error
    # suggests loads against a real catalog and beats an agent allow.
    authz = _provider(
        locked=[{"what": _BASH, "effect": "deny"}],
        rules=_PLATFORM_RULES,
        catalog=_catalog(
            ("kolsch", "bash"), ("kolsch", "read_file"), ("kolsch", "write_file")
        ),
    )
    result = await authz.allow(
        "user.local",
        _BASH,
        target="rm -rf /tmp/x",
        layers=(PermissionLayer(name="agent", rules=_ALLOW_BASH),),
    )
    _assert_locked_deny(result)


# ---------------------------------------------------------------------------
# N3 — the compile choke point refuses a non-deny outright
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("effect", ["allow", "prompt"])
def test_compile_choke_point_refuses_non_deny(effect: str) -> None:
    authz = _provider(locked=[])
    with pytest.raises(
        AuthzMalformedRule, match=r"locked_rules\[0\]: effect must be 'deny'"
    ):
        authz._compile_locked_fields(
            0, {"what": _BASH, "pattern": "*", "effect": effect}, source=None
        )
