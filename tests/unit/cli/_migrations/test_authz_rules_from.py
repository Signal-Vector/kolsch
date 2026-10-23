"""bug-159 — the authz-rules migrator never drops `from` / `pattern`.

Before bug-159 the legacy→canonical heuristics returned a fresh
`{what, pattern, effect}` dict, so a legacy rule scoped with `from:`
was rewritten to `platform.yaml` as an unscoped rule (a catch-all
`allow` became an unconditional allow-all). Now a valid `from:` is
carried through; an invalid `from:` or a non-`*` `pattern:` keeps the
rule legacy + deprecated, and the provider refuses it at load.

Also pins the `_rule_to_dict` → YAML → provider → `list_rules`
round-trip (AC 9).
"""

from __future__ import annotations

from typing import Any

import pytest

from assets.catalog import AssetCatalog, AssetEntry
from assets.manifest import BundleManifest
from cli._migrations.authz_rules import migrate_authz_rules
from cli.permissions import _rule_to_dict
from errors import AuthzMalformedRule
from kolsch._yamlio import dump, load
from providers.authz import Rule, RuleBasedAuthorizationProvider

A = "kolsch/agents/a"


def _catalog() -> AssetCatalog:
    entries = [
        ("kolsch/tools/bash", "tool", "tools/bash.py"),
        ("kolsch/agents/a", "agent", "agents/a.yaml"),
        ("kolsch/workflows/w", "workflow", "workflows/w.yaml"),
    ]
    assets = {
        ref: AssetEntry(ref=ref, asset_type=kind, bundle="kolsch", path=path)
        for ref, kind, path in entries
    }
    bundles = {
        "kolsch": BundleManifest(
            name="kolsch",
            version="0.1.0",
            description="kolsch bundle (test fixture)",
            path="/tmp/bundles/kolsch",
        )
    }
    return AssetCatalog(bundles=bundles, assets=assets)


def _legacy(what: str, effect: str = "allow", **extra: Any) -> dict[str, Any]:
    return {"who": ".*", "what": what, "effect": effect} | extra


# ---------------------------------------------------------------------------
# Valid `from:` is carried through
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("what", "expected_what", "catalog"),
    [
        (".*", "*", None),
        (r"agent\.execute\..*", "agent.execute.*", None),
        (r"tool\.bash", "kolsch/tools/bash", "catalog"),
    ],
    ids=["catch_all", "agent_execute", "tool_pin"],
)
def test_valid_from_is_carried_through(
    what: str, expected_what: str, catalog: str | None
) -> None:
    result = migrate_authz_rules(
        [_legacy(what, **{"from": A})],
        catalog=_catalog() if catalog else None,
    )
    assert result.rules == [
        {"what": expected_what, "pattern": "*", "from": A, "effect": "allow"}
    ]
    assert list(result.rules[0]) == ["what", "pattern", "from", "effect"]
    assert result.deprecated == []


def test_catch_all_from_never_becomes_unscoped_allow_all() -> None:
    """The deep-dive probe: was `{what: *, pattern: *, effect: allow}`."""
    result = migrate_authz_rules([_legacy(".*", **{"from": A})])
    assert result.rules[0].get("from") == A


def test_from_underscore_key_is_carried_as_from() -> None:
    result = migrate_authz_rules([_legacy(".*", from_=A)])
    assert result.rules == [{"what": "*", "pattern": "*", "from": A, "effect": "allow"}]


@pytest.mark.parametrize("ref", ["kolsch/agents/*", "kolsch/workflows/w"])
def test_glob_and_workflow_from_pass_with_catalog(ref: str) -> None:
    result = migrate_authz_rules(
        [_legacy(".*", "deny", **{"from": ref})], catalog=_catalog()
    )
    assert result.rules[0]["from"] == ref


def test_bash_suggest_from_is_carried_through() -> None:
    result = migrate_authz_rules(
        [_legacy("^git .*$", **{"from": A})], catalog=_catalog()
    )
    assert result.rules == [
        {"what": "kolsch/tools/bash", "pattern": "git *", "from": A, "effect": "allow"}
    ]


def test_migrated_scoped_rule_loads_and_enforces_scope() -> None:
    result = migrate_authz_rules([_legacy(".*", **{"from": A})])
    provider = RuleBasedAuthorizationProvider(config={"rules": result.rules})
    assert provider._rules[0].raw.from_ == A


# ---------------------------------------------------------------------------
# Invalid `from:` / non-`*` `pattern:` → keep legacy + deprecated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("from_value", "needle"),
    [
        ("not-a-ref", "3-segment"),
        ("kolsch/tools/bash", "asset type 'tools'"),
        ("kolsch/agents/missing", "not in the catalog"),
        (42, "3-segment"),
    ],
)
def test_invalid_from_keeps_rule_legacy(from_value: object, needle: str) -> None:
    raw = _legacy(".*", **{"from": from_value})
    result = migrate_authz_rules([raw], catalog=_catalog())
    assert result.rules == [raw]
    assert len(result.deprecated) == 1
    entry = result.deprecated[0]
    assert entry.kind == "regex"
    assert "`from:" in entry.reason
    assert needle in entry.reason


def test_missing_from_target_passes_without_catalog() -> None:
    """Catalog existence is only checked when a catalog is supplied."""
    result = migrate_authz_rules([_legacy(".*", **{"from": "kolsch/agents/missing"})])
    assert result.rules[0]["from"] == "kolsch/agents/missing"


def test_non_star_pattern_keeps_rule_legacy() -> None:
    raw = _legacy(".*", pattern="safe*")
    result = migrate_authz_rules([raw])
    assert result.rules == [raw]
    assert "`pattern: 'safe*'`" in result.deprecated[0].reason


def test_star_pattern_on_legacy_still_migrates() -> None:
    result = migrate_authz_rules([_legacy(".*", pattern="*")])
    assert result.rules == [{"what": "*", "pattern": "*", "effect": "allow"}]


def test_unconvertible_regex_with_from_names_from() -> None:
    raw = _legacy("^[a-z]+$", **{"from": A})
    result = migrate_authz_rules([raw])
    assert result.rules == [raw]
    assert "`from:`" in result.deprecated[0].reason
    assert "migration heuristic" in result.deprecated[0].reason


def test_kept_legacy_rule_fails_provider_load() -> None:
    """The intended loud failure for a rule the migrator could not convert."""
    result = migrate_authz_rules([_legacy(".*", **{"from": "not-a-ref"})])
    with pytest.raises(AuthzMalformedRule):
        RuleBasedAuthorizationProvider(config={"rules": result.rules})


def test_unscoped_legacy_migration_unchanged() -> None:
    result = migrate_authz_rules(
        [_legacy(".*"), _legacy(r"tool\.bash", "prompt"), _legacy(".*", "deny")],
        catalog=_catalog(),
    )
    assert result.rules == [
        {"what": "*", "pattern": "*", "effect": "allow"},
        {"what": "kolsch/tools/bash", "pattern": "*", "effect": "prompt"},
        {"what": "*", "pattern": "*", "effect": "deny"},
    ]


# ---------------------------------------------------------------------------
# AC 9 — `_rule_to_dict` round-trip preserves `from` / `pattern`
# ---------------------------------------------------------------------------


async def test_rule_to_dict_round_trip_preserves_from_and_pattern() -> None:
    rules = [
        Rule(what="kolsch/tools/bash", pattern="git *", from_=A, effect="allow"),
        Rule(what="kolsch/tools/bash", pattern="ls *", effect="prompt"),
        Rule(what="agent.execute.*", from_="kolsch/workflows/*", effect="allow"),
        Rule(id="rl_custom", who=".*", what=r"tool\.x", effect="deny"),
        Rule(what="*", pattern="*", effect="deny"),
    ]
    text = dump({"rules": [_rule_to_dict(r) for r in rules]})
    provider = RuleBasedAuthorizationProvider(config=load(text))
    reloaded = await provider.list_rules()
    assert [(r.what, r.pattern, r.from_, r.who, r.effect) for r in reloaded] == [
        (r.what, r.pattern, r.from_, r.who, r.effect) for r in rules
    ]
