"""Authorization provider errors — spec §4.9 + §4.16 (Phase 2.0).

Phase 1 surface (§4.9): authorization *denial* is NOT an error (it
is a structured ``AuthorizationResult``); only configuration problems
produce errors. Both Phase 1 codes are Fatal, raised at init.

Phase 2.0 surface (§4.16, TOOL-R5): the ``prompt`` effect and the
live-edit CLI introduce six new codes:

- ``authz.denied`` (Recoverable) — the tools dispatcher's surface
  for a rule-based denial. The model sees this in its tool-result
  payload.
- ``authz.prompt_unattended`` (Recoverable) — non-chat callers
  collapse ``prompt`` to deny and emit this so replay shows the
  question that would have been asked.
- ``authz.rule_invalid`` (Fatal) — CLI ``permissions add``
  validation failure.
- ``authz.rule_not_found`` (Fatal) — CLI ``permissions remove`` /
  ``show`` for an unknown id.
- ``authz.protected_rule`` (Fatal) — ``permissions remove
  rl_default_deny`` (the tail is removal-protected).
"""

from errors.base import PlatformError
from errors.level import Level


class AuthzMalformedRule(PlatformError):
    """A rule entry is malformed (missing fields, wrong shape)."""

    code = "authz.malformed_rule"
    level = Level.FATAL

    def __init__(
        self,
        rule_index: int,
        error: str,
        *,
        where: str = "authz.rule_based.load",
    ) -> None:
        super().__init__(
            message=f"Malformed authorization rule at index {rule_index}: {error}",
            where=where,
            detail={"rule_index": rule_index, "error": error},
        )


class AuthzRegexInvalid(PlatformError):
    """A regex in `who` or `what` failed to compile (TC-AUZ-05)."""

    code = "authz.regex_invalid"
    level = Level.FATAL

    def __init__(
        self,
        rule_index: int,
        field: str,
        regex: str,
        error: str,
        *,
        where: str = "authz.rule_based.compile",
    ) -> None:
        super().__init__(
            message=(
                f"Invalid regex in rule {rule_index} field {field!r}: "
                f"{regex!r} ({error})"
            ),
            where=where,
            detail={
                "rule_index": rule_index,
                "field": field,
                "regex": regex,
                "error": error,
            },
        )


# ---------------------------------------------------------------------------
# Phase 2.0 (TOOL-R5) — error-model §4.16
# ---------------------------------------------------------------------------


class AuthzDenied(PlatformError):
    """Tools dispatcher denied a call per a rule's ``deny`` effect."""

    code = "authz.denied"
    level = Level.RECOVERABLE

    def __init__(
        self,
        tool: str,
        target: str | None,
        rule_id: str | None,
        *,
        where: str = "tools.dispatch.invoke",
    ) -> None:
        super().__init__(
            message=(
                f"Authorization denied tool {tool!r}"
                + (f" against target {target!r}" if target else "")
                + (f" by rule {rule_id}" if rule_id else " by default deny")
            ),
            where=where,
            detail={"tool": tool, "target": target, "rule_id": rule_id},
        )


class AuthzPromptUnattended(PlatformError):
    """Non-chat caller collapsed a ``prompt`` rule to deny.

    Recoverable — the model sees the deny in its tool-result payload
    and reacts. The detail records the caller for replay analysis.
    """

    code = "authz.prompt_unattended"
    level = Level.RECOVERABLE

    def __init__(
        self,
        tool: str,
        target: str | None,
        rule_id: str | None,
        caller: str,
        *,
        where: str = "tools.dispatch.invoke",
    ) -> None:
        super().__init__(
            message=(
                f"Prompt rule matched for tool {tool!r}"
                + (f" target {target!r}" if target else "")
                + f"; no interactive callback ({caller}); collapsed to deny"
            ),
            where=where,
            detail={
                "tool": tool,
                "target": target,
                "rule_id": rule_id,
                "caller": caller,
            },
        )


class AuthzRuleInvalid(PlatformError):
    """``kolsch permissions add`` rejected the user-supplied rule."""

    code = "authz.rule_invalid"
    level = Level.FATAL

    def __init__(
        self,
        position: int,
        reason: str,
        *,
        where: str = "cli.permissions.add",
    ) -> None:
        super().__init__(
            message=f"Invalid rule: {reason}",
            where=where,
            detail={"position": position, "reason": reason},
        )


class AuthzRuleNotFound(PlatformError):
    """``kolsch permissions {remove, show}`` for an unknown rule id."""

    code = "authz.rule_not_found"
    level = Level.FATAL

    def __init__(
        self,
        rule_id: str,
        available_ids: list[str],
        *,
        where: str = "authz.rule_based.get_rule",
    ) -> None:
        super().__init__(
            message=(
                f"No rule with id {rule_id!r}; available: "
                f"{', '.join(available_ids) if available_ids else '(none)'}"
            ),
            where=where,
            detail={"id": rule_id, "available_ids": available_ids},
        )


class AuthzProtectedRule(PlatformError):
    """``kolsch permissions remove rl_default_deny`` — TOOL-R10."""

    code = "authz.protected_rule"
    level = Level.FATAL

    def __init__(
        self,
        rule_id: str,
        reason: str,
        *,
        where: str = "authz.rule_based.remove_rule",
    ) -> None:
        super().__init__(
            message=f"Rule {rule_id!r} is removal-protected: {reason}",
            where=where,
            detail={"id": rule_id, "reason": reason},
        )


# ---------------------------------------------------------------------------
# qol-permission-system-human-readable.md Step 2 — schema migration
# ---------------------------------------------------------------------------


class AuthzBareToolRef(PlatformError):
    """Rule's ``what`` is a bare tool name, not the qualified form.

    QoL — human-readable permission system, decision 6: ``what`` must
    name the tool by its fully-qualified ref (``<bundle>/<name>``)
    even when no ambiguity exists. ``what: bash`` is rejected at
    load with a clear error pointing the user at the qualified form
    they should use.
    """

    code = "authz.bare_tool_ref"
    level = Level.FATAL

    def __init__(
        self,
        rule_index: int,
        bare_name: str,
        candidates: list[str],
        *,
        where: str = "authz.rule_based.validate",
    ) -> None:
        super().__init__(
            message=(
                f"Rule at index {rule_index}: `what: {bare_name!r}` is a "
                f"bare tool name; use the fully-qualified form. "
                f"Candidate(s): {candidates}"
            ),
            where=where,
            detail={
                "rule_index": rule_index,
                "bare_name": bare_name,
                "candidates": list(candidates),
            },
        )


class AuthzPatternInvalid(PlatformError):
    """Rule's ``pattern`` contains characters reserved for regex.

    QoL — human-readable permission system, decision 3: ``pattern``
    is a glob, not a regex. Characters that introduce regex semantics
    (``[``, ``]``, ``^``, ``$``, ``\\``, ``(``, ``)``, ``+``, ``|``,
    ``{``, ``}``) are rejected at config load so a legacy regex
    pattern can't be silently miscompiled as a glob.
    """

    code = "authz.pattern_invalid"
    level = Level.FATAL

    def __init__(
        self,
        rule_index: int,
        pattern: str,
        forbidden_chars: list[str],
        *,
        where: str = "authz.rule_based.validate",
    ) -> None:
        super().__init__(
            message=(
                f"Rule at index {rule_index}: pattern {pattern!r} contains "
                f"regex character(s) {forbidden_chars}; patterns are globs "
                f"(use `*` for any-chars, `?` for one-char)"
            ),
            where=where,
            detail={
                "rule_index": rule_index,
                "pattern": pattern,
                "forbidden_chars": list(forbidden_chars),
            },
        )


class AuthzToolRefUnknown(PlatformError):
    """Rule's ``what`` is a qualified tool ref but no such tool exists.

    QoL — human-readable permission system, decision 6: a slash-form
    ``what`` (``kolsch/bash``) must resolve to a real tool in the
    catalog. Missing tool → load failure with a clear name.
    """

    code = "authz.tool_ref_unknown"
    level = Level.FATAL

    def __init__(
        self,
        rule_index: int,
        tool_ref: str,
        *,
        where: str = "authz.rule_based.validate",
    ) -> None:
        super().__init__(
            message=(
                f"Rule at index {rule_index}: `what: {tool_ref!r}` does not "
                f"resolve to any catalogued tool"
            ),
            where=where,
            detail={"rule_index": rule_index, "tool_ref": tool_ref},
        )


class ConfigDeprecatedRegexRule(PlatformError):
    """A platform.yaml rule still uses the legacy regex shape.

    QoL — human-readable permission system, decision 11: rules that
    don't fit the migration heuristic (custom user regex with
    character classes, lookahead, etc.) stay in legacy form during
    the migration window. The platform loads them but emits this
    Recoverable warning at workspace load so the user knows to
    convert them.
    """

    code = "config.deprecated_regex_rule"
    level = Level.RECOVERABLE

    def __init__(
        self,
        rule_index: int,
        what: str,
        reason: str,
        *,
        where: str = "cli._migrations.authz_rules",
    ) -> None:
        super().__init__(
            message=(
                f"Rule at index {rule_index} kept in legacy regex form "
                f"(what={what!r}): {reason}. Convert manually or via "
                f"`/perm` slash commands."
            ),
            where=where,
            detail={
                "rule_index": rule_index,
                "what": what,
                "reason": reason,
            },
        )


class ConfigDeprecatedRuleForm(PlatformError):
    """A platform.yaml rule uses the deprecated 2-seg ``<bundle>/<name>`` form.

    qol-action-namespace-bundle-scoped.md Step 2 — the 2-seg shape
    shipped by the human-readable QoL is superseded by the 3-seg
    catalog ref ``<bundle>/tools/<name>``. The startup migration
    shim auto-rewrites these where the bundle owns a catalogued
    tool of that name; entries that can't be auto-rewritten (no
    catalog, ambiguous bundle, ref points at a non-tool asset)
    survive in 2-seg form and emit this Recoverable warning so
    the user knows to convert them. The platform still loads the
    2-seg form during the migration window — warning-only for at
    least one minor version.
    """

    code = "config.deprecated_rule_form"
    level = Level.RECOVERABLE

    def __init__(
        self,
        rule_index: int,
        what: str,
        reason: str,
        *,
        where: str = "cli._migrations.authz_rules",
    ) -> None:
        super().__init__(
            message=(
                f"Rule at index {rule_index} uses deprecated rule form "
                f"(what={what!r}): {reason}."
            ),
            where=where,
            detail={
                "rule_index": rule_index,
                "what": what,
                "reason": reason,
            },
        )
