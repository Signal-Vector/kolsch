"""Authz-rules migration — qol-permission-system-human-readable.md Step 2.

Converts the pre-QoL rule shape ``{id, who, what, effect}`` (regex
``what`` matched against action OR target) to the new shape
``{what, pattern, effect}`` (glob ``what`` matched against action OR
tool ref; glob ``pattern`` matched against target). Pure function;
no I/O — the caller reads / writes ``platform.yaml``.

Migration heuristics (Step 1 writeup §2, mirroring the table in the
spec's Part 1 §10):

1. ``what: tool\\.<name>`` (the legacy action-namespace pin) →
   ``what: <home_bundle>/<name>, pattern: "*"``. ``home_bundle`` is
   resolved by walking the catalog for a tool entry whose final
   segment matches ``<name>``. Zero hits or two-plus hits make the
   rule un-migratable; it stays in legacy form and emits a
   :class:`DeprecatedRegexRule` event for the workspace-load
   surface.
2. ``what: ^<cmd> .*$`` (the legacy bash-suggest output) →
   ``what: <bash_bundle>/bash, pattern: "<cmd> *"``. Same fallback
   when the workspace has no ``bash`` tool (e.g., the user removed
   the kolsch bundle).
3. ``what: agent\\.execute\\..*`` (the engine pre-flight allow) →
   ``what: "agent.execute.*", pattern: "*"``. The trailing glob in
   ``what`` mirrors the original regex's ``\\..*`` clause so
   ``fnmatch("agent.execute.helper", "agent.execute.*")`` fires the
   same as the legacy ``re.fullmatch``.
4. ``what: .*`` (the legacy catch-all) →
   ``what: "*", pattern: "*"``.

Unconvertible rules — anything containing character classes,
lookahead, or other regex constructs the heuristic doesn't recognise
— stay in legacy form and emit a Recoverable
``config.deprecated_regex_rule`` event so the user knows to convert
them. The rule still loads, so the platform isn't bricked by an
unfamiliar regex.
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
    """One legacy regex rule that couldn't be auto-converted.

    Carries enough context for the workspace-load surface to emit a
    :class:`errors.ConfigDeprecatedRegexRule` Recoverable event. The
    actual emission is the caller's responsibility — this dataclass
    keeps the migrator pure (no error-class instantiation in the
    hot path).
    """

    rule_index: int
    what: str
    reason: str


@dataclass(frozen=True)
class MigrationResult:
    """Output of :func:`migrate_authz_rules`.

    ``rules`` is the post-migration rule list ready to write back to
    ``platform.yaml`` (or pass to
    :class:`providers.authz.RuleBasedAuthorizationProvider`).
    ``deprecated`` lists every rule that stayed in legacy form so
    the caller can emit the matching Recoverable events.
    """

    rules: list[dict[str, Any]]
    deprecated: list[DeprecatedRegexRule] = field(default_factory=list)


def migrate_authz_rules(
    rules: list[dict[str, Any]],
    *,
    catalog: AssetCatalog | None = None,
) -> MigrationResult:
    """Convert a legacy rule list to the new shape.

    Rules already in the new shape (no ``who`` field) pass through
    untouched — including ``id``-less entries the user may have
    hand-authored. Legacy rules (``who`` field present) run through
    the four-branch heuristic above; unconvertible entries stay in
    legacy form and are recorded in :attr:`MigrationResult.deprecated`.

    Args:
        rules: list of YAML-loaded rule dicts.
        catalog: optional :class:`AssetCatalog`. Required for the
            ``tool\\.<name>`` and ``^<cmd> .*$`` branches; absent
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
            # Already new-shape — pass through unchanged.
            converted.append(_strip_legacy_keys(raw))
            continue
        # Legacy shape. Try each heuristic in priority order.
        migrated = _try_migrate(raw, catalog=catalog)
        if migrated is None:
            deprecated.append(
                DeprecatedRegexRule(
                    rule_index=index,
                    what=str(raw.get("what", "")),
                    reason=_diagnose_legacy(raw, catalog=catalog),
                ),
            )
            converted.append(raw)  # keep legacy form
            continue
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
    """Return a new-shape rule dict, or ``None`` if no heuristic fires.

    The caller treats ``None`` as "leave in legacy form + record
    as deprecated." The heuristic priority matches Step 1's table.
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

    # 3. tool.<name> action pin → qualified ref. Needs catalog.
    tool_match = _TOOL_ACTION_RE.match(what)
    if tool_match:
        if catalog is None:
            return None
        name = tool_match.group(1)
        bundle = _resolve_tool_home_bundle(catalog, name)
        if bundle is None:
            return None
        return {
            "what": f"{bundle}/{name}",
            "pattern": "*",
            "effect": effect,
        }

    # 4. bash-suggest `^<cmd> .*$` → bash tool + `"<cmd> *"`.
    bash_match = _BASH_SUGGEST_RE.match(what)
    if bash_match:
        if catalog is None:
            return None
        bundle = _resolve_tool_home_bundle(catalog, "bash")
        if bundle is None:
            return None
        cmd = bash_match.group(1)
        return {
            "what": f"{bundle}/bash",
            "pattern": f"{cmd} *",
            "effect": effect,
        }

    return None


def _resolve_tool_home_bundle(catalog: AssetCatalog, name: str) -> str | None:
    """Find the bundle that owns a tool by short name.

    Returns ``None`` when zero or two-plus bundles ship the tool —
    both cases make the migration ambiguous and the rule survives
    in legacy form (the user resolves by hand or via ``/perm``).
    """
    matches = [
        entry.bundle
        for entry in catalog.assets.values()
        if entry.asset_type == "tool" and entry.ref.rsplit("/", 1)[-1] == name
    ]
    if len(matches) != 1:
        return None
    return matches[0]


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
            "`.*` — Phase 3 principal scoping is not yet supported"
        )
    if _TOOL_ACTION_RE.match(what):
        if catalog is None:
            return "tool.<name> action pin needs a catalog to resolve"
        tool_match = _TOOL_ACTION_RE.match(what)
        if tool_match is not None:
            name = tool_match.group(1)
            return (
                f"tool {name!r} not found in catalog or ambiguous "
                "across bundles"
            )
    if _BASH_SUGGEST_RE.match(what):
        if catalog is None:
            return "bash-suggest pattern needs a catalog to resolve"
        return "bash tool not found in catalog"
    return "regex doesn't match any migration heuristic"


def _strip_legacy_keys(raw: dict[str, Any]) -> dict[str, Any]:
    """Remove vestigial ``id`` / ``who`` from a new-shape entry.

    Defensive: hand-authored new-shape YAML sometimes carries a
    stray ``id:`` or ``who:`` field copied from an older example.
    Strip them so the post-migration YAML is canonical.
    """
    return {k: v for k, v in raw.items() if k not in ("id", "who")}
