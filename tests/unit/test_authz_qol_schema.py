"""qol-permission-system-human-readable.md Step 2 — unit tests.

Five paths the spec calls out:

1. New-shape rule loads and matches via glob.
2. Legacy-shape rule still loads + matches via regex (backward compat).
3. Bare-tool-name in `what` → rejected at provider construction.
4. Ambiguous bare-tool-name → :class:`AssetsAmbiguousShorthand`.
5. Glob matches the canonical operators (`*`, `?`, `<prefix>*`).

Plus migration-shim coverage:

6. The four heuristic branches each emit the right new-shape rule.
7. Unconvertible regex rules survive in legacy form + are recorded
   in the result's ``deprecated`` list.

Tests use a hand-built :class:`AssetCatalog` (no workspace on disk) so
they stay in the `unit/` tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from assets.catalog import AssetCatalog, AssetEntry
from assets.manifest import BundleManifest
from cli._migrations.authz_rules import migrate_authz_rules
from errors import (
    AssetsAmbiguousShorthand,
    AuthzBareToolRef,
    AuthzMalformedRule,
    AuthzPatternInvalid,
    AuthzToolRefUnknown,
)
from providers import Rule, RuleBasedAuthorizationProvider


# ---------------------------------------------------------------------------
# Catalog fixtures
# ---------------------------------------------------------------------------


def _catalog(*tool_refs: tuple[str, str]) -> AssetCatalog:
    """Build a minimal :class:`AssetCatalog` with the given tools.

    Each tuple is ``(bundle, name)``. We synthesise just enough
    metadata for the authz validator's catalog lookups; the rest of
    the catalog scanning logic isn't exercised here.
    """
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


# ---------------------------------------------------------------------------
# 1. New-shape rule loads + matches via glob
# ---------------------------------------------------------------------------


async def test_new_shape_rule_matches_against_tool_ref() -> None:
    """A `what: kolsch/tools/bash` rule fires on a `kolsch/tools/bash` action.

    qol-action-namespace-bundle-scoped.md Step 2 — the action namespace
    and the rule `what:` share the same 3-seg grammar; the matcher
    hits action directly without the deleted bridge map.
    """
    catalog = _catalog(("kolsch", "bash"))
    provider = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"what": "kolsch/tools/bash", "pattern": "*", "effect": "allow"},
            ],
        },
        catalog=catalog,
    )
    result = await provider.allow("user.local", "kolsch/tools/bash", target="ls -la")
    assert result.allowed
    assert result.matched_rule is not None
    assert result.matched_rule.what == "kolsch/tools/bash"


async def test_new_shape_pattern_narrows_by_target() -> None:
    """`pattern: "find *"` matches only `find ...` bash commands."""
    catalog = _catalog(("kolsch", "bash"))
    provider = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"what": "kolsch/tools/bash", "pattern": "find *", "effect": "allow"},
                {"what": "*", "pattern": "*", "effect": "deny"},
            ],
        },
        catalog=catalog,
    )
    matched = await provider.allow(
        "u", "kolsch/tools/bash", target="find / -name foo"
    )
    assert matched.allowed

    blocked = await provider.allow("u", "kolsch/tools/bash", target="rm -rf /")
    assert not blocked.allowed
    assert blocked.matched_rule is not None
    assert blocked.matched_rule.what == "*"


async def test_new_shape_system_action_namespace() -> None:
    """`what: agent.execute.*` fires on `agent.execute.<id>`."""
    catalog = _catalog()
    provider = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"what": "agent.execute.*", "pattern": "*", "effect": "allow"},
            ],
        },
        catalog=catalog,
    )
    result = await provider.allow("user.local", "agent.execute.helper")
    assert result.allowed


# ---------------------------------------------------------------------------
# 2. Legacy-shape rule still loads + matches (backward compat)
# ---------------------------------------------------------------------------


async def test_legacy_shape_still_loads_and_matches() -> None:
    """A workspace with the pre-QoL rule shape still loads + fires.

    The provider's compile step dispatches on ``who``'s presence;
    legacy rules use the existing ``re.fullmatch`` semantics."""
    provider = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"who": ".*", "what": r"tool\.bash", "effect": "allow"},
            ],
        },
    )
    result = await provider.allow("u", "tool.bash")
    assert result.allowed
    assert result.matched_rule is not None
    assert result.matched_rule.who == ".*"


async def test_legacy_and_new_shape_coexist_in_one_provider() -> None:
    """Mixed-shape rule list compiles + matches both halves.

    The legacy regex rule still matches its 1-seg action (kept for
    workspaces mid-migration); the new-shape rule matches the 3-seg
    action directly (qol-action-namespace-bundle-scoped.md Step 2).
    """
    catalog = _catalog(("kolsch", "bash"))
    provider = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                # Legacy — still matches the legacy 1-seg action.
                {"who": ".*", "what": r"tool\.read_file", "effect": "allow"},
                # New 3-seg shape.
                {"what": "kolsch/tools/bash", "pattern": "*", "effect": "allow"},
            ],
        },
        catalog=catalog,
    )
    read_ok = await provider.allow("u", "tool.read_file", target="/tmp/x")
    assert read_ok.allowed
    bash_ok = await provider.allow("u", "kolsch/tools/bash", target="ls")
    assert bash_ok.allowed


# ---------------------------------------------------------------------------
# 3-4. Validation at provider construction
# ---------------------------------------------------------------------------


def test_bare_tool_name_rejected_with_candidates() -> None:
    """`what: bash` (bare) → AuthzBareToolRef naming the 3-seg form.

    qol-action-namespace-bundle-scoped.md Step 2 — candidates are the
    canonical 3-seg catalog refs (was 2-seg `kolsch/bash`).
    """
    catalog = _catalog(("kolsch", "bash"))
    with pytest.raises(AuthzBareToolRef) as excinfo:
        RuleBasedAuthorizationProvider(
            config={"rules": [{"what": "bash", "pattern": "*", "effect": "allow"}]},
            catalog=catalog,
        )
    assert excinfo.value.detail["bare_name"] == "bash"
    assert "kolsch/tools/bash" in excinfo.value.detail["candidates"]


def test_bare_tool_name_ambiguous_across_bundles() -> None:
    """Two bundles ship `bash` + a rule has `what: bash` → ambiguous error."""
    catalog = _catalog(("kolsch", "bash"), ("andre", "bash"))
    with pytest.raises(AssetsAmbiguousShorthand) as excinfo:
        RuleBasedAuthorizationProvider(
            config={"rules": [{"what": "bash", "pattern": "*", "effect": "allow"}]},
            catalog=catalog,
        )
    assert set(excinfo.value.detail["candidates"]) == {
        "kolsch/tools/bash",
        "andre/tools/bash",
    }


def test_qualified_tool_ref_not_in_catalog_rejected() -> None:
    """`what: kolsch/missing` → AuthzToolRefUnknown."""
    catalog = _catalog(("kolsch", "bash"))
    with pytest.raises(AuthzToolRefUnknown):
        RuleBasedAuthorizationProvider(
            config={
                "rules": [
                    {"what": "kolsch/missing", "pattern": "*", "effect": "allow"},
                ],
            },
            catalog=catalog,
        )


def test_pattern_with_regex_chars_rejected() -> None:
    """`pattern: "^find .*$"` → AuthzPatternInvalid listing the chars."""
    with pytest.raises(AuthzPatternInvalid) as excinfo:
        RuleBasedAuthorizationProvider(
            config={
                "rules": [
                    {"what": "*", "pattern": "^find .*$", "effect": "allow"},
                ],
            },
        )
    forbidden = excinfo.value.detail["forbidden_chars"]
    assert "^" in forbidden
    assert "$" in forbidden


async def test_catch_all_what_star_accepted() -> None:
    """`what: "*"` (the new-shape catch-all) compiles + fires."""
    provider = RuleBasedAuthorizationProvider(
        config={
            "rules": [{"what": "*", "pattern": "*", "effect": "deny"}],
        },
    )
    result = await provider.allow("u", "tool.anything")
    assert not result.allowed


# ---------------------------------------------------------------------------
# 5. Glob matches `*` / `?` / `<prefix>*`
# ---------------------------------------------------------------------------


async def test_glob_star_matches_anything() -> None:
    catalog = _catalog(("kolsch", "bash"))
    provider = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"what": "kolsch/tools/bash", "pattern": "*", "effect": "allow"},
            ],
        },
        catalog=catalog,
    )
    for cmd in ("ls", "find / -x", ""):
        result = await provider.allow("u", "kolsch/tools/bash", target=cmd)
        assert result.allowed, cmd


async def test_glob_question_matches_one_char() -> None:
    catalog = _catalog(("kolsch", "bash"))
    provider = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"what": "kolsch/tools/bash", "pattern": "l?", "effect": "allow"},
                {"what": "*", "pattern": "*", "effect": "deny"},
            ],
        },
        catalog=catalog,
    )
    assert (await provider.allow("u", "kolsch/tools/bash", target="ls")).allowed
    assert not (
        await provider.allow("u", "kolsch/tools/bash", target="lsa")
    ).allowed


async def test_glob_prefix_matches() -> None:
    catalog = _catalog(("kolsch", "bash"))
    provider = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"what": "kolsch/tools/bash", "pattern": "find *", "effect": "allow"},
                {"what": "*", "pattern": "*", "effect": "deny"},
            ],
        },
        catalog=catalog,
    )
    assert (
        await provider.allow("u", "kolsch/tools/bash", target="find / -name x")
    ).allowed
    assert not (
        await provider.allow("u", "kolsch/tools/bash", target="rm -rf /")
    ).allowed


# ---------------------------------------------------------------------------
# 6. Migration heuristics
# ---------------------------------------------------------------------------


def test_migration_converts_legacy_catch_all() -> None:
    """`{who:.*, what:.*, effect:deny}` → `{what:"*", pattern:"*", effect:deny}`."""
    out = migrate_authz_rules(
        [{"who": ".*", "what": ".*", "effect": "deny"}],
    )
    assert out.rules == [{"what": "*", "pattern": "*", "effect": "deny"}]
    assert out.deprecated == []


def test_migration_converts_agent_execute() -> None:
    out = migrate_authz_rules(
        [{"who": ".*", "what": r"agent\.execute\..*", "effect": "allow"}],
    )
    assert out.rules == [
        {"what": "agent.execute.*", "pattern": "*", "effect": "allow"},
    ]


def test_migration_converts_tool_action_pin() -> None:
    """Legacy `tool\\.<name>` → 3-seg `<bundle>/tools/<name>`."""
    catalog = _catalog(("kolsch", "read_file"))
    out = migrate_authz_rules(
        [{"who": ".*", "what": r"tool\.read_file", "effect": "allow"}],
        catalog=catalog,
    )
    assert out.rules == [
        {"what": "kolsch/tools/read_file", "pattern": "*", "effect": "allow"},
    ]


def test_migration_converts_bash_suggest() -> None:
    """The legacy `_modal.suggest_pattern` output round-trips to 3-seg."""
    catalog = _catalog(("kolsch", "bash"))
    out = migrate_authz_rules(
        [{"who": ".*", "what": r"^find .*$", "effect": "allow"}],
        catalog=catalog,
    )
    assert out.rules == [
        {"what": "kolsch/tools/bash", "pattern": "find *", "effect": "allow"},
    ]


def test_migration_keeps_unconvertible_in_legacy_form() -> None:
    """A custom regex with character classes survives untouched."""
    catalog = _catalog(("kolsch", "bash"))
    out = migrate_authz_rules(
        [{"who": ".*", "what": r"^[a-z]+ \w*$", "effect": "allow"}],
        catalog=catalog,
    )
    # Survives in legacy form.
    assert out.rules[0]["who"] == ".*"
    # Recorded as deprecated.
    assert len(out.deprecated) == 1
    assert out.deprecated[0].rule_index == 0


def test_migration_passes_new_shape_through_when_3seg() -> None:
    """3-seg new-shape rules are untouched (no `who`, no `pattern` rewrite)."""
    out = migrate_authz_rules(
        [{"what": "kolsch/tools/bash", "pattern": "*", "effect": "allow"}],
    )
    assert out.rules == [
        {"what": "kolsch/tools/bash", "pattern": "*", "effect": "allow"},
    ]
    assert out.deprecated == []


def test_migration_upgrades_two_seg_to_three_seg() -> None:
    """qol-action-namespace-bundle-scoped.md Step 2 — 2-seg new-shape rules
    auto-upgrade to 3-seg at startup; the deprecation is recorded."""
    catalog = _catalog(("kolsch", "bash"))
    out = migrate_authz_rules(
        [{"what": "kolsch/bash", "pattern": "*", "effect": "allow"}],
        catalog=catalog,
    )
    assert out.rules == [
        {"what": "kolsch/tools/bash", "pattern": "*", "effect": "allow"},
    ]
    assert len(out.deprecated) == 1
    assert out.deprecated[0].kind == "two_seg"
    assert out.deprecated[0].what == "kolsch/bash"


def test_migration_two_seg_unresolvable_survives_with_warning() -> None:
    """2-seg refs that don't resolve to a tool stay as-is + warn."""
    catalog = _catalog(("kolsch", "bash"))
    out = migrate_authz_rules(
        [{"what": "andre/unknown", "pattern": "*", "effect": "allow"}],
        catalog=catalog,
    )
    # Survives in 2-seg form (catalog doesn't own this tool).
    assert out.rules == [
        {"what": "andre/unknown", "pattern": "*", "effect": "allow"},
    ]
    assert len(out.deprecated) == 1
    assert out.deprecated[0].kind == "two_seg"


def test_migration_reattributes_orphaned_3seg_to_owner_bundle() -> None:
    """bug-authz-rule-caller-bundle-mismatch — orphaned 3-seg refs.

    A rule with `what: andre/tools/read_file` gets rewritten to
    `kolsch/tools/read_file` when the catalog says `read_file` lives
    in `kolsch`. Simulates the modal-granted rule the butler agent
    produces after inheriting `read_file` from
    `kolsch/agents/helper`.
    """
    catalog = _catalog(("kolsch", "read_file"))
    out = migrate_authz_rules(
        [{"what": "andre/tools/read_file", "pattern": "*", "effect": "allow"}],
        catalog=catalog,
    )
    assert out.rules == [
        {"what": "kolsch/tools/read_file", "pattern": "*", "effect": "allow"},
    ]
    assert len(out.deprecated) == 1
    assert out.deprecated[0].kind == "orphan_3seg"
    assert out.deprecated[0].what == "andre/tools/read_file"
    assert "bug-authz-rule-caller-bundle-mismatch" in out.deprecated[0].reason


def test_migration_orphaned_3seg_ambiguous_leaves_alone() -> None:
    """When 2+ bundles own the same tool name, don't guess — leave alone.

    Migrator must not silently mis-attribute in the ambiguous case.
    The rule survives unchanged; boot-time authz will still fail
    but the user gets a chance to fix by hand.
    """
    catalog = _catalog(("kolsch", "bash"), ("otherbundle", "bash"))
    out = migrate_authz_rules(
        [{"what": "andre/tools/bash", "pattern": "*", "effect": "allow"}],
        catalog=catalog,
    )
    # Unchanged.
    assert out.rules == [
        {"what": "andre/tools/bash", "pattern": "*", "effect": "allow"},
    ]
    # No deprecation event either — nothing was actionable.
    assert out.deprecated == []


def test_migration_orphaned_3seg_no_matching_tool_leaves_alone() -> None:
    """If no bundle owns the referenced tool, survive unchanged.

    Could be a real user-authored tool that was later removed, or a
    typo. The migrator has no honest way to fix it; the user does.
    """
    catalog = _catalog(("kolsch", "read_file"))
    out = migrate_authz_rules(
        [
            {
                "what": "andre/tools/fetch_website",
                "pattern": "*",
                "effect": "allow",
            }
        ],
        catalog=catalog,
    )
    assert out.rules == [
        {"what": "andre/tools/fetch_website", "pattern": "*", "effect": "allow"},
    ]
    assert out.deprecated == []


def test_migration_valid_3seg_ref_passes_through_untouched() -> None:
    """A correctly-attributed 3-seg ref is not deprecation-flagged."""
    catalog = _catalog(("kolsch", "read_file"))
    out = migrate_authz_rules(
        [{"what": "kolsch/tools/read_file", "pattern": "*", "effect": "allow"}],
        catalog=catalog,
    )
    assert out.rules == [
        {"what": "kolsch/tools/read_file", "pattern": "*", "effect": "allow"},
    ]
    assert out.deprecated == []


def test_migration_full_canonical_workspace_roundtrip() -> None:
    """Every rule from the canonical platform.yaml migrates cleanly."""
    catalog = _catalog(
        ("kolsch", "bash"),
        ("kolsch", "read_file"),
        ("kolsch", "list_files"),
        ("kolsch", "write_file"),
    )
    legacy = [
        {"id": "rl_a", "who": ".*", "what": r"agent\.execute\..*", "effect": "allow"},
        {"id": "rl_b", "who": ".*", "what": r"tool\.read_file", "effect": "allow"},
        {"id": "rl_c", "who": ".*", "what": r"tool\.list_files", "effect": "allow"},
        {"id": "rl_d", "who": ".*", "what": r"tool\.write_file", "effect": "prompt"},
        {"id": "rl_e", "who": ".*", "what": r"^find .*$", "effect": "allow"},
        {"id": "rl_f", "who": ".*", "what": r"^ls .*$", "effect": "allow"},
        {"id": "rl_g", "who": ".*", "what": r"^grep .*$", "effect": "allow"},
        {"id": "rl_h", "who": ".*", "what": r"^cat .*$", "effect": "allow"},
        {"id": "rl_i", "who": ".*", "what": r"tool\.bash", "effect": "prompt"},
        {"id": "rl_default_deny", "who": ".*", "what": r".*", "effect": "deny"},
    ]
    out = migrate_authz_rules(legacy, catalog=catalog)
    assert out.deprecated == []
    expected = [
        {"what": "agent.execute.*", "pattern": "*", "effect": "allow"},
        {"what": "kolsch/tools/read_file", "pattern": "*", "effect": "allow"},
        {"what": "kolsch/tools/list_files", "pattern": "*", "effect": "allow"},
        {"what": "kolsch/tools/write_file", "pattern": "*", "effect": "prompt"},
        {"what": "kolsch/tools/bash", "pattern": "find *", "effect": "allow"},
        {"what": "kolsch/tools/bash", "pattern": "ls *", "effect": "allow"},
        {"what": "kolsch/tools/bash", "pattern": "grep *", "effect": "allow"},
        {"what": "kolsch/tools/bash", "pattern": "cat *", "effect": "allow"},
        {"what": "kolsch/tools/bash", "pattern": "*", "effect": "prompt"},
        {"what": "*", "pattern": "*", "effect": "deny"},
    ]
    assert out.rules == expected


async def test_migrated_rules_load_into_provider_and_match() -> None:
    """The migration output composes with the provider end-to-end.

    qol-action-namespace-bundle-scoped.md Step 2 — migrated rules
    are 3-seg `<bundle>/tools/<name>`; the runtime action is the
    same 3-seg form; no bridge needed.
    """
    catalog = _catalog(("kolsch", "bash"))
    legacy = [
        {"who": ".*", "what": r"^find .*$", "effect": "allow"},
        {"who": ".*", "what": r"tool\.bash", "effect": "prompt"},
        {"who": ".*", "what": r".*", "effect": "deny"},
    ]
    migrated = migrate_authz_rules(legacy, catalog=catalog)
    provider = RuleBasedAuthorizationProvider(
        config={"rules": migrated.rules},
        catalog=catalog,
    )
    # The `find *` rule should fire and allow.
    res = await provider.allow("u", "kolsch/tools/bash", target="find / -name x")
    assert res.allowed
    # Any other bash command falls through to `prompt` then `deny`
    # (no callback → prompt collapses to deny).
    res = await provider.allow("u", "kolsch/tools/bash", target="rm -rf /")
    assert not res.allowed


# ---------------------------------------------------------------------------
# qol-action-namespace-bundle-scoped.md Step 2 regression coverage
# ---------------------------------------------------------------------------


async def test_cross_bundle_tools_are_distinct_authz_subjects() -> None:
    """qol-action-namespace-bundle-scoped.md acceptance #7 — the
    headline win: two bundles shipping a tool of the same name now
    have distinct rule subjects. ``kolsch/tools/bash`` and
    ``andre/tools/bash`` are NOT bridged by any translation map."""
    catalog = _catalog(("kolsch", "bash"), ("andre", "bash"))
    provider = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"what": "kolsch/tools/bash", "pattern": "*", "effect": "allow"},
                {"what": "andre/tools/bash", "pattern": "*", "effect": "deny"},
            ],
        },
        catalog=catalog,
    )
    kolsch_ok = await provider.allow("u", "kolsch/tools/bash", target="ls")
    andre_blocked = await provider.allow("u", "andre/tools/bash", target="ls")
    assert kolsch_ok.allowed
    assert not andre_blocked.allowed
    # And the rules cross-fire test: an `andre` rule does NOT match
    # a `kolsch` action and vice versa.
    assert kolsch_ok.matched_rule is not None
    assert kolsch_ok.matched_rule.what == "kolsch/tools/bash"
    assert andre_blocked.matched_rule is not None
    assert andre_blocked.matched_rule.what == "andre/tools/bash"


async def test_translation_map_no_longer_carries_tool_entries() -> None:
    """qol-action-namespace-bundle-scoped.md acceptance #4 — the
    ``_build_action_to_tool_ref`` tool branch is deleted. The map
    still exists for node entries (separate ticket); for tools it
    should be empty."""
    catalog = _catalog(("kolsch", "bash"), ("andre", "bash"))
    provider = RuleBasedAuthorizationProvider(
        config={"rules": []},
        catalog=catalog,
    )
    # Tool actions: no bridge entries.
    assert provider.lookup_tool_ref("tool.bash") is None
    assert provider.lookup_tool_ref("kolsch/tools/bash") is None
    assert provider.lookup_tool_ref("andre/tools/bash") is None


# ---------------------------------------------------------------------------
# 7. Rule class behaviour
# ---------------------------------------------------------------------------


def test_rule_new_shape_constructs_without_who() -> None:
    """The new-shape Rule omits `who` entirely."""
    r = Rule(what="kolsch/bash", pattern="ls *", effect="allow")
    assert r.who is None
    assert r.is_legacy is False
    assert r.pattern == "ls *"


def test_rule_legacy_shape_still_constructs() -> None:
    """Existing call sites that pass `who=` keep working."""
    r = Rule(who=".*", what=r"tool\.bash", effect="prompt")
    assert r.who == ".*"
    assert r.is_legacy is True


def test_rule_pattern_default_is_star() -> None:
    r = Rule(what="*", effect="deny")
    assert r.pattern == "*"


def test_rule_rejects_unknown_extra_field() -> None:
    """The model still has `extra="forbid"` — typos surface clearly."""
    with pytest.raises(ValidationError):
        Rule(what="*", pattern="*", effect="allow", typo="x")
