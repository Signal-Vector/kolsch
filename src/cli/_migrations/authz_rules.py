r"""Authz-rules in-place migration.

Converts the legacy rule shape `{id, who, what, effect}` (regex
`what` matched against action OR target) to the current shape
`{what, pattern, effect}` (glob `what` matched against the 3-seg
catalog ref; glob `pattern` matched against target). Pure function;
no I/O — the caller reads / writes `platform.yaml`.

Migration heuristics (in priority order; first match wins):

1. `what: tool\.<name>` (legacy action-namespace pin) →
   `what: <home_bundle>/tools/<name>, pattern: "*"`. `home_bundle`
   is resolved by walking the catalog for a tool entry whose final
   segment matches `<name>`. Zero hits or two-plus hits make the
   rule un-migratable; it stays in legacy form and emits a
   [`DeprecatedRegexRule`][cli._migrations.authz_rules.DeprecatedRegexRule]
   event tagged `regex` for the workspace-load surface.
2. `what: ^<cmd> .*$` (legacy bash-suggest output) →
   `what: <bash_bundle>/tools/bash, pattern: "<cmd> *"`. Same
   fallback when the workspace has no `bash` tool.
3. `what: agent\.execute\..*` (engine pre-flight allow) →
   `what: "agent.execute.*", pattern: "*"`. Trailing glob mirrors
   the original regex's `\..*` clause.
4. `what: .*` (legacy catch-all) → `what: "*", pattern: "*"`.
5. `what: <bundle>/<name>` (the deprecated 2-seg new-shape form) →
   `what: <bundle>/tools/<name>` (3-seg). Catalog must confirm the
   bundle owns a tool of that name; ambiguous / unknown refs stay
   in 2-seg form and emit a deprecation event tagged `two_seg`.
   Only fires on new-shape rules (`pattern` field present); legacy
   rules go through heuristic 1.

Caller-chain scoping (`from:`) is carried through, never dropped. A
legacy rule cannot express `from:` (the provider refuses one that
carries it), so a legacy rule with a `from:` that passes the
provider's shape check (3 segments, asset type `agents` /
`workflows`, catalog existence for a non-glob name when a catalog is
supplied) converts to the canonical dict with the same `from:`. A
legacy rule whose `from:` fails that check, or that carries a
non-`*` `pattern:`, stays in legacy form and is recorded as
deprecated — the provider then refuses it at load, loudly, instead
of the migrator writing a rule wider than its input.

Unconvertible rules — anything containing character classes,
lookahead, or other regex constructs the heuristic doesn't
recognise, or 2-seg refs that don't resolve in the catalog — stay
in their existing form and emit a Recoverable event so the user
knows to convert them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from assets.catalog import AssetCatalog


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeprecatedRegexRule:
    """A rule the migrator couldn't auto-convert or converted from a deprecated form.

    The `kind` discriminator tells the emitter which event class to
    raise:

    - `kind="regex"` (default — legacy 1-seg regex shape) →
      [`ConfigDeprecatedRegexRule`][errors.ConfigDeprecatedRegexRule].
    - `kind="two_seg"` (new-shape rule using the deprecated 2-seg
      `<bundle>/<name>` form) →
      [`ConfigDeprecatedRuleForm`][errors.ConfigDeprecatedRuleForm].

    Both shapes were either converted (the rule was rewritten) or
    survive in their input form; the dataclass entry exists so the
    workspace-load surface can emit a Recoverable warning per
    affected rule.
    """

    rule_index: int
    what: str
    reason: str
    kind: str = "regex"


@dataclass(frozen=True)
class MigrationResult:
    """Output of the authz-rules migrator.

    `rules` is the post-migration rule list ready to write back to
    `platform.yaml` (or pass to
    [`RuleBasedAuthorizationProvider`][providers.RuleBasedAuthorizationProvider]).
    `deprecated` lists every rule that stayed in legacy form so the
    caller can emit the matching Recoverable events.
    """

    rules: list[dict[str, Any]]
    deprecated: list[DeprecatedRegexRule] = field(default_factory=list)


def migrate_authz_rules(
    rules: list[Any],
    *,
    catalog: AssetCatalog | None = None,
) -> MigrationResult:
    r"""Convert a legacy rule list to the new shape.

    Rules already in the new shape (no `who` field) pass through
    untouched — including `id`-less entries the user may have
    hand-authored. Legacy rules (`who` field present) run through
    the four-branch heuristic above; unconvertible entries stay in
    legacy form and are recorded in `MigrationResult.deprecated`.

    Args:
        rules: YAML-loaded rule dicts.
        catalog: Optional asset catalog. Required for the
            `tool\.<name>` and `^<cmd> .*$` branches; absent
            catalog forces those rules into the deprecated bucket.
    """
    converted: list[dict[str, Any]] = []
    deprecated: list[DeprecatedRegexRule] = []
    for index, raw in enumerate(rules):
        if not isinstance(raw, dict):
            # Pass through; the provider's compile will reject it
            # with AuthzMalformedRule at load time, with a clearer
            # error than anything the migrator could emit.
            converted.append(raw)
            continue
        if "who" not in raw:
            # Already new-shape. Two orthogonal migrations apply:
            #
            # 1. Orphaned 3-seg reattribution
            #    (bug-authz-rule-caller-bundle-mismatch). If `what`
            #    is `<bundle>/tools/<name>` but that bundle doesn't
            #    own a tool of that name AND another bundle does,
            #    rewrite the prefix so boot-time authz strictness
            #    lets it through.
            # 2. 2-seg → 3-seg upgrade. Legacy 2-seg refs
            #    `<bundle>/<name>` rewrite to `<bundle>/tools/<name>`
            #    when the catalog confirms the bundle owns the tool.
            #
            # (1) fires before (2) because it targets a shape (3-seg)
            # that (2) leaves untouched.
            stripped = _strip_legacy_keys(raw)
            reattributed, did_reattribute = _try_reattribute_orphaned_3seg(
                stripped, catalog=catalog
            )
            if reattributed is not None:
                converted.append(reattributed)
                if did_reattribute:
                    deprecated.append(
                        DeprecatedRegexRule(
                            rule_index=index,
                            what=str(stripped.get("what", "")),
                            reason=(
                                "3-seg ref pointed at a bundle that does "
                                "not own the tool; auto-rewrote to the "
                                f"bundle that does ({reattributed['what']}). "
                                "This usually means the rule was granted via "
                                "the modal or `/perm allow` on a tool an "
                                "agent inherited from another bundle "
                                "(see bug-authz-rule-caller-bundle-mismatch)."
                            ),
                            kind="orphan_3seg",
                        ),
                    )
                continue
            upgraded = _try_upgrade_two_seg(stripped, catalog=catalog)
            if upgraded is not None:
                converted.append(upgraded)
                if upgraded != stripped:
                    deprecated.append(
                        DeprecatedRegexRule(
                            rule_index=index,
                            what=str(stripped.get("what", "")),
                            reason=(
                                "2-seg form `<bundle>/<name>` is deprecated; "
                                "use the 3-seg catalog ref "
                                "`<bundle>/tools/<name>` instead"
                            ),
                            kind="two_seg",
                        ),
                    )
                continue
            # Looks 2-seg but unresolvable — pass through unchanged
            # and warn the user.
            converted.append(stripped)
            if _looks_like_two_seg(str(stripped.get("what", ""))):
                deprecated.append(
                    DeprecatedRegexRule(
                        rule_index=index,
                        what=str(stripped.get("what", "")),
                        reason=_diagnose_two_seg(stripped, catalog=catalog),
                        kind="two_seg",
                    ),
                )
            continue
        # Legacy shape. A `from:` / non-`*` `pattern:` the legacy
        # shape can't express must survive conversion or block it —
        # never be dropped (dropping widens the rule).
        scope_reason = _diagnose_legacy_scope(raw, catalog=catalog)
        migrated = None if scope_reason else _try_migrate(raw, catalog=catalog)
        if migrated is None:
            reason = scope_reason or _diagnose_legacy(raw, catalog=catalog)
            if scope_reason is None and _legacy_from(raw) is not None:
                reason += (
                    "; the rule also carries `from:`, which the legacy "
                    "shape cannot express — it will fail to load until "
                    "converted to the canonical shape by hand"
                )
            deprecated.append(
                DeprecatedRegexRule(
                    rule_index=index,
                    what=str(raw.get("what", "")),
                    reason=reason,
                    kind="regex",
                ),
            )
            converted.append(raw)  # keep legacy form
            continue
        from_value = _legacy_from(raw)
        if from_value is not None:
            migrated = {
                "what": migrated["what"],
                "pattern": migrated["pattern"],
                "from": from_value,
                "effect": migrated["effect"],
            }
        converted.append(migrated)
    return MigrationResult(rules=converted, deprecated=deprecated)


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------


# `what: tool\.<name>` (with optional anchors)
_TOOL_ACTION_RE = re.compile(r"^\^?tool\\\.([a-z0-9][a-z0-9_-]*)\$?$")
# `what: ^<cmd> .*$` (bash-suggest output from _modal.suggest_pattern)
_BASH_SUGGEST_RE = re.compile(r"^\^([a-zA-Z0-9_\-./]+) \.\*\$$")
# `what: agent\.execute\..*` (engine pre-flight allow)
_AGENT_EXECUTE_RE = re.compile(r"^\^?agent\\\.execute\\\.\.\*\$?$")
# `what: .*` (catch-all)
_CATCHALL_RE = re.compile(r"^\^?\.\*\$?$")


def _try_migrate(
    raw: dict[str, Any],
    *,
    catalog: AssetCatalog | None,
) -> dict[str, Any] | None:
    """Return a new-shape rule dict, or `None` if no heuristic fires.

    The caller treats `None` as "leave in legacy form + record as
    deprecated." The heuristic priority matches the module header's
    list.
    """
    what = raw.get("what")
    effect = raw.get("effect")
    if not isinstance(what, str) or not isinstance(effect, str):
        return None
    # Honour the spec's "only `who: .*` exists today" finding —
    # anything else is hand-authored and survives in legacy form
    # rather than risk silently scoping the rule wrong.
    if raw.get("who") != ".*":
        return None

    # 1. Catch-all comes first so `.*` doesn't get misread as a
    # tool-action pattern.
    if _CATCHALL_RE.match(what):
        return {"what": "*", "pattern": "*", "effect": effect}

    # 2. agent.execute namespace.
    if _AGENT_EXECUTE_RE.match(what):
        return {"what": "agent.execute.*", "pattern": "*", "effect": effect}

    # 3. tool.<name> action pin → 3-seg catalog ref. Needs catalog.
    # Target shape is `<bundle>/tools/<name>` (was 2-seg
    # `<bundle>/<name>` in older versions).
    tool_match = _TOOL_ACTION_RE.match(what)
    if tool_match:
        if catalog is None:
            return None
        name = tool_match.group(1)
        bundle = _resolve_tool_home_bundle(catalog, name)
        if bundle is None:
            return None
        return {
            "what": f"{bundle}/tools/{name}",
            "pattern": "*",
            "effect": effect,
        }

    # 4. bash-suggest `^<cmd> .*$` → bash tool + `"<cmd> *"`.
    # Same 3-seg upgrade applies.
    bash_match = _BASH_SUGGEST_RE.match(what)
    if bash_match:
        if catalog is None:
            return None
        bundle = _resolve_tool_home_bundle(catalog, "bash")
        if bundle is None:
            return None
        cmd = bash_match.group(1)
        return {
            "what": f"{bundle}/tools/bash",
            "pattern": f"{cmd} *",
            "effect": effect,
        }

    return None


# The 2-seg new-shape form (`<bundle>/<name>`) is being deprecated.
# Anchored to start-of-string + end so we don't confuse 2-seg with
# 3-seg (`<bundle>/tools/<name>`) which contains an extra `/`.
_TWO_SEG_GLOB_RE = re.compile(r"^([a-z0-9][a-z0-9_-]*)/([a-z][a-z0-9_-]*)$")


def _looks_like_two_seg(what: str) -> bool:
    """Return True when `what` matches the deprecated 2-seg shape exactly."""
    return _TWO_SEG_GLOB_RE.match(what) is not None


def _try_upgrade_two_seg(
    raw: dict[str, Any],
    *,
    catalog: AssetCatalog | None,
) -> dict[str, Any] | None:
    """Rewrite a new-shape rule whose `what` is 2-seg → 3-seg.

    Returns the rewritten dict when the 2-seg ref resolves to a
    catalogued tool, or the original dict unchanged when the rule
    isn't 2-seg (passes through). Returns `None` to signal the
    caller "looked 2-seg but couldn't resolve" — caller then keeps
    the original rule and emits a deprecation event.

    The function is conservative: anything other than an exact
    2-seg `<bundle>/<name>` match is treated as non-2-seg and
    returned untouched (3-seg refs, glob patterns like
    `kolsch/*`, system actions like `agent.execute.*`).
    """
    what = raw.get("what")
    if not isinstance(what, str):
        return raw
    match = _TWO_SEG_GLOB_RE.match(what)
    if match is None:
        # Not 2-seg — leave as-is (3-seg, glob, system action, etc).
        return raw
    if catalog is None:
        # Looks 2-seg but we can't verify; surface for deprecation
        # event but don't rewrite (caller emits the warning).
        return None
    bundle, name = match.group(1), match.group(2)
    if not _bundle_owns_tool(catalog, bundle, name):
        # Bundle doesn't own a tool of that name — could be a
        # genuine non-tool ref (skills, agents are also 2-seg
        # `<bundle>/<name>` in their own contexts). Surface for
        # deprecation event without rewriting.
        return None
    rewritten = dict(raw)
    rewritten["what"] = f"{bundle}/tools/{name}"
    return rewritten


def _bundle_owns_tool(catalog: AssetCatalog, bundle: str, name: str) -> bool:
    """Return True when `<bundle>` ships a tool entry whose short name is `<name>`."""
    return any(
        entry.asset_type == "tool"
        and entry.bundle == bundle
        and entry.ref.rsplit("/", 1)[-1] == name
        for entry in catalog.assets.values()
    )


def _diagnose_two_seg(
    raw: dict[str, Any],
    *,
    catalog: AssetCatalog | None,
) -> str:
    """Short reason for a 2-seg rule that survived in deprecated form."""
    what = str(raw.get("what", ""))
    if catalog is None:
        return (
            "2-seg form `<bundle>/<name>` is deprecated; "
            "catalog unavailable to auto-rewrite to 3-seg form"
        )
    match = _TWO_SEG_GLOB_RE.match(what)
    if match is None:
        return "shape not 2-seg; no diagnosis available"
    bundle, name = match.group(1), match.group(2)
    return (
        f"2-seg form is deprecated and bundle {bundle!r} does not "
        f"own a tool named {name!r} (could be a skill / agent ref); "
        "rewrite manually as `<bundle>/tools/<name>` if it pins a tool"
    )


def _resolve_tool_home_bundle(catalog: AssetCatalog, name: str) -> str | None:
    """Find the bundle that owns a tool by short name.

    Returns `None` when zero or two-plus bundles ship the tool —
    both cases make the migration ambiguous and the rule survives
    in legacy form (the user resolves by hand or via `/perm`).
    """
    matches = [
        entry.bundle
        for entry in catalog.assets.values()
        if entry.asset_type == "tool" and entry.ref.rsplit("/", 1)[-1] == name
    ]
    if len(matches) != 1:
        return None
    return matches[0]


# Full 3-seg tool ref anchored at start and end (`<bundle>/tools/<name>`).
# Distinct from `_TWO_SEG_GLOB_RE` — the middle segment must be literal
# `tools`. Used by the orphaned-3-seg reattribution path.
_THREE_SEG_TOOL_RE = re.compile(r"^([a-z0-9][a-z0-9_-]*)/tools/([a-z][a-z0-9_-]*)$")


def _try_reattribute_orphaned_3seg(
    raw: dict[str, Any],
    *,
    catalog: AssetCatalog | None,
) -> tuple[dict[str, Any] | None, bool]:
    """Rewrite a 3-seg rule pointing at a bundle that doesn't own the tool.

    Fixes bug-authz-rule-caller-bundle-mismatch: modal-granted rules
    on inherited tools were persisted under the CALLER agent's bundle
    instead of the tool's authoritative home bundle. Boot on strict
    authz then rejects the ref with `AuthzToolRefUnknown` because
    `<caller_bundle>/tools/<name>` doesn't resolve.

    Returns `(rewritten_or_None, was_reattributed)`:

    - `(new_dict, True)` — matched an orphaned 3-seg ref AND a
      same-named tool exists in exactly one other bundle. Rewrote
      the `what:` prefix to that bundle so the rule resolves at
      boot. Caller should record a deprecation event.
    - `(None, False)` — rule doesn't look like an orphaned 3-seg
      ref, OR the same-named tool is missing / ambiguous. Caller
      passes the rule through unchanged.

    Ambiguous cases (0 or ≥2 same-named tools across bundles) are
    intentionally left alone — the user resolves by hand or via
    `/perm`. Silently guessing a bundle would mis-attribute the
    rule again in a different direction.
    """
    what = raw.get("what")
    if not isinstance(what, str):
        return None, False
    match = _THREE_SEG_TOOL_RE.match(what)
    if match is None:
        return None, False
    if catalog is None:
        # Can't verify without the catalog — leave as-is; the
        # boot-time validator will produce a clearer error than any
        # migrator guess.
        return None, False
    claimed_bundle, name = match.group(1), match.group(2)
    if _bundle_owns_tool(catalog, claimed_bundle, name):
        # Ref is valid — nothing to do.
        return None, False
    # Ref is orphaned. Try to reattribute if a same-named tool lives
    # in exactly one other bundle.
    real_bundle = _resolve_tool_home_bundle(catalog, name)
    if real_bundle is None:
        # Zero or ≥2 candidates — leave alone.
        return None, False
    rewritten = dict(raw)
    rewritten["what"] = f"{real_bundle}/tools/{name}"
    return rewritten, True


# `from:` caller refs: `<bundle>/<agents|workflows>/<name>`.
_FROM_REF_PARTS = 3
_FROM_ASSET_SEGMENTS = {"agents": "agent", "workflows": "workflow"}


def _legacy_from(raw: dict[str, Any]) -> object:
    """Return a legacy entry's `from` (YAML key) or `from_` value, else `None`."""
    return raw.get("from", raw.get("from_"))


def _diagnose_legacy_scope(
    raw: dict[str, Any],
    *,
    catalog: AssetCatalog | None,
) -> str | None:
    """Return why a legacy rule's `pattern` / `from` blocks conversion, or `None`.

    Mirrors the provider's `_validate_from` shape check (3 segments,
    asset type `agents` / `workflows`, catalog existence for a
    non-glob name when a catalog is supplied) so a `from:` that
    passes here also loads once converted.
    """
    pattern = raw.get("pattern", "*")
    if pattern != "*":
        return (
            f"legacy rule carries `pattern: {pattern!r}`, which the legacy "
            "shape ignores; convert it to the canonical shape by hand — "
            "the rule will fail to load until then"
        )
    from_value = _legacy_from(raw)
    if from_value is None:
        return None
    parts = from_value.split("/") if isinstance(from_value, str) else []
    if len(parts) != _FROM_REF_PARTS or not all(parts):
        return (
            f"`from: {from_value!r}` is not a 3-segment "
            "`<bundle>/agents/<name>` or `<bundle>/workflows/<name>` ref; "
            "fix it and convert the rule by hand — it will fail to load "
            "until then"
        )
    _bundle, segment, name = parts
    expected_type = _FROM_ASSET_SEGMENTS.get(segment)
    if expected_type is None:
        return (
            f"`from: {from_value!r}` names asset type {segment!r}; only "
            "`agents` and `workflows` can be callers — fix it and convert "
            "the rule by hand; it will fail to load until then"
        )
    if (
        catalog is not None
        and "*" not in name
        and "?" not in name
        and catalog.lookup(str(from_value), expected_type=expected_type) is None
    ):
        return (
            f"`from: {from_value!r}` is not in the catalog; fix it and "
            "convert the rule by hand — it will fail to load until then"
        )
    return None


def _diagnose_legacy(
    raw: dict[str, Any],
    *,
    catalog: AssetCatalog | None,
) -> str:
    """Short reason string for a deprecated-rule event.

    Two common cases: the heuristic didn't match (custom regex), or
    a catalog lookup failed. The reason is for the user; precise
    enough to act on without forcing them to read this module.
    """
    what = raw.get("what", "")
    if not isinstance(what, str):
        return "unsupported `what` value"
    if raw.get("who") != ".*":
        return (
            f"`who: {raw.get('who')!r}` is not the default-principal "
            "`.*` — principal scoping is not yet supported"
        )
    if _TOOL_ACTION_RE.match(what):
        if catalog is None:
            return "tool.<name> action pin needs a catalog to resolve"
        tool_match = _TOOL_ACTION_RE.match(what)
        if tool_match is not None:
            name = tool_match.group(1)
            return f"tool {name!r} not found in catalog or ambiguous across bundles"
    if _BASH_SUGGEST_RE.match(what):
        if catalog is None:
            return "bash-suggest pattern needs a catalog to resolve"
        return "bash tool not found in catalog"
    return "regex doesn't match any migration heuristic"


def _strip_legacy_keys(raw: dict[str, Any]) -> dict[str, Any]:
    """Remove vestigial `id` / `who` from a new-shape entry.

    Defensive: hand-authored new-shape YAML sometimes carries a
    stray `id:` or `who:` field copied from an older example.
    Strip them so the post-migration YAML is canonical.
    """
    return {k: v for k, v in raw.items() if k not in ("id", "who")}
