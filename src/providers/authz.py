"""Authorization provider — first-match-wins ACL with default-deny.

The `rule_based` variant is a real ACL implementation from day one.
Two rule schemas coexist during an in-flight migration: the canonical
`{what, pattern, effect}` glob-matched shape and the legacy
`{id, who, what, effect}` regex-matched shape. The matcher dispatches
per-shape; a well-formed rule set terminates at the protected
default-deny tail
[`DEFAULT_DENY_RULE_ID`][providers.authz.DEFAULT_DENY_RULE_ID]. The
tail is not enforced as present: a set without it falls through to the
implicit no-rule path (prompt when a callback is registered, else
deny). [`inspect_default_deny_tail`][providers.authz.inspect_default_deny_tail]
reports where (if anywhere) the tail sits.

The `effect` field carries three values — `allow`, `deny`, and
`prompt`. The provider classifies; on `prompt` it either calls the
supplied resolver callback or collapses to `deny` for unattended
callers. The live-edit surface (`add_rule` / `remove_rule` /
`get_rule` on the rule-based variant) backs the `kolsch permissions`
CLI and the in-chat modal.

For the full contract — principal grammar, layered walk, audit
emission, prompt resolution — see the [secrets-sandbox-authz
contract](../explanation/secrets-sandbox-authz-contract.md).

A separate `AllowAllTestAuthorizationProvider` variant returns
`ALLOW` for everything; it is registered unconditionally but its
constructor refuses to run unless the `KOLSCH_TEST_BUILD=1` env var
is set.
"""

from __future__ import annotations

import fnmatch
import functools
import hashlib
import inspect
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assets.refs import build_ref, is_three_segment, parse_three_segment
from errors import (
    AuthzBareToolRef,
    AuthzMalformedRule,
    AuthzPatternInvalid,
    AuthzRegexInvalid,
    AuthzToolRefUnknown,
    ConfigDeprecatedRegexRule,
    ConfigInvalid,
)
from errors.assets import AssetsAmbiguousShorthand
from files.authz_target import path_pattern_matches
from schemas.config import legacy_rule_scope_violation
from workflows.errors import (
    WorkflowFromAssetTypeError,
    WorkflowFromNotFound,
    WorkflowFromShapeError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from assets.catalog import AssetCatalog
    from engine.context import ExecutionContext
    from files.authz_target import PathTarget


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DEFAULT_DENY_RULE_ID = "rl_default_deny"
"""The reserved id of the default-deny tail.

Set at parse time when a rule with the matching shape is detected;
[`RuleBasedAuthorizationProvider.remove_rule`][providers.authz.RuleBasedAuthorizationProvider.remove_rule]
refuses to remove this id (even when no rule carries it). The shapes
that earn this id are the **unscoped** catch-all denies: legacy
`who=".*"` AND `what=".*"` AND `effect="deny"`, or canonical `what="*"`
AND `pattern="*"` AND `effect="deny"` with no `from`. A `from`-scoped
catch-all deny gets an ordinary `rl_…` id (see
[`derive_rule_id`][providers.authz.derive_rule_id]). To check whether a
rule set actually ends with the tail, use
[`inspect_default_deny_tail`][providers.authz.inspect_default_deny_tail]."""


# feat-041 — sidecar shape passed by config loaders that use
# `_yamlio.load_with_positions` to record where each rule was
# authored. Keyed by the rule's index in the source YAML list; value
# is `(absolute_source_file_path, 1-indexed_line_number)`. Consumed
# by `RuleBasedAuthorizationProvider.__init__` and by the
# `PermissionRule → Rule` conversion in `engine.py` where the
# `extra="forbid"` PermissionRule schema forbids inline fields.
RuleSourceMap = dict[int, tuple[str, int]]


_log = logging.getLogger("providers.authz")


KOLSCH_TEST_BUILD_ENV_VAR = "KOLSCH_TEST_BUILD"
"""Env var that, set to `"1"`, enables the `allow_all_test` variant.

Production builds never set it; setting it on a non-test runtime is a
configuration error."""


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class AuthzEffect(StrEnum):
    """The three classifications a rule can return."""

    ALLOW = "allow"
    DENY = "deny"
    PROMPT = "prompt"


# Canonical segment counts for the two ref grammars in use today.
# Three-segment `<bundle>/<plural>/<name>` is the universal canonical
# form; two-segment `<bundle>/<name>` is the legacy tool-ref migration
# shim. Named constants here so the segment-count checks read intent
# rather than literal magic numbers (ruff PLR2004).
_THREE_SEG_PARTS = 3
_TWO_SEG_PARTS = 2

# Legacy `what:` regex shapes the `kolsch setup` migrator
# (`cli._migrations.authz_rules`) knows how to convert; it imports these
# so the grammar lives in one place.
# `tool\.<name>` (with optional anchors) — the pre-3-seg tool action pin.
LEGACY_TOOL_ACTION_RE = re.compile(r"^\^?tool\\\.([a-z0-9][a-z0-9_-]*)\$?$")
# `^<cmd> .*$` — bash-suggest output from `_modal.suggest_pattern`.
LEGACY_BASH_SUGGEST_RE = re.compile(r"^\^([a-zA-Z0-9_\-./]+) \.\*\$$")
# `agent\.execute\..*` — the engine pre-flight allow.
LEGACY_AGENT_EXECUTE_RE = re.compile(r"^\^?agent\\\.execute\\\.\.\*\$?$")
# `.*` — the catch-all.
LEGACY_CATCHALL_RE = re.compile(r"^\^?\.\*\$?$")
_LEGACY_MIGRATABLE_WHATS = (
    LEGACY_CATCHALL_RE,
    LEGACY_AGENT_EXECUTE_RE,
    LEGACY_TOOL_ACTION_RE,
    LEGACY_BASH_SUGGEST_RE,
)


def _entry_from_value(index: int, entry: dict[str, Any]) -> Any:  # noqa: ANN401 — raw YAML value
    """Return a raw rule entry's `from` value, reading both spellings.

    YAML uses `from`; programmatic dicts may use the Python name
    `from_`. Either non-null key scopes the rule (a present-but-null
    `from` must not hide a `from_`). Both set to different values is
    ambiguous and refused.
    """
    from_value = entry.get("from")
    alt_value = entry.get("from_")
    if from_value is None:
        return alt_value
    if alt_value is not None and alt_value != from_value:
        raise AuthzMalformedRule(
            rule_index=index,
            error=(
                f"rule sets both `from: {from_value!r}` and "
                f"`from_: {alt_value!r}`; keep one (`from`)"
            ),
        )
    return from_value


def _setup_can_migrate_legacy(entry: dict[str, Any], from_value: object) -> bool:
    """Return True when `kolsch setup` would convert this scoped legacy rule.

    Mirrors the migrator's acceptance: `who: ".*"`, no non-`*`
    `pattern`, a `from` of shape `<bundle>/<agents|workflows>/<name>`,
    and a `what` one of its heuristics recognises. (Catalog checks —
    tool home bundle, `from` existence — are left to setup, which warns
    when they fail.)
    """
    if entry.get("who") != ".*" or entry.get("pattern", "*") != "*":
        return False
    if not isinstance(from_value, str):
        return False
    parts = from_value.split("/")
    if len(parts) != _THREE_SEG_PARTS or not all(parts):
        return False
    if parts[1] not in ("agents", "workflows"):
        return False
    what = entry.get("what")
    return isinstance(what, str) and any(
        regex.match(what) for regex in _LEGACY_MIGRATABLE_WHATS
    )


def _build_action_to_tool_ref(catalog: AssetCatalog) -> dict[str, str]:
    """Build the action-to-ref bridge map used by the new-shape matcher.

    Tool actions are now 3-segment catalog refs
    (`<bundle>/tools/<name>`), so a rule
    `what: kolsch/tools/bash` matches the action invocation directly
    via `fnmatch` — no bridge needed. The cross-bundle name-collision
    ambiguity the previous tool branch carried is structurally
    impossible now: the bundle segment is always present in the action
    string.

    The node branch stays until the parallel unification for workflow
    nodes ships. Node actions are still 1-segment `node.<name>`; the
    rule's `what:` carries the canonical 3-segment
    `<bundle>/nodes/<name>`; the matcher uses this map's node entries
    to fnmatch the two forms.
    """
    out: dict[str, str] = {}
    for entry in catalog.assets.values():
        if entry.asset_type == "node":
            # Workflow node actions land at `node.<name>`; rules in
            # `what:` carry the canonical 3-seg `<bundle>/nodes/<name>`
            # ref. The action map bridges so a rule
            # `what: kolsch/nodes/bash` fires on a `node.bash`
            # invocation.
            name = entry.ref.rsplit("/", 1)[-1]
            out[f"node.{name}"] = entry.ref
    return out


_PATTERN_REGEX_CHARS: tuple[str, ...] = (
    "[",
    "]",
    "^",
    "$",
    "\\",
    "(",
    ")",
    "+",
    "|",
    "{",
    "}",
)
"""Characters rejected in new-shape `pattern` fields.

`pattern` is a glob, not a regex. Accepting any of these would either
silently miscompile a legacy regex as a glob (e.g. `^find .*$` becomes
a literal-never-matches string) or expose fnmatch's character-class
extension (`[abc]`) that confuses readers familiar with regex
character classes. Both are footguns the schema migration is meant to
eliminate."""


class Rule(BaseModel):
    """One ACL rule.

    The canonical shape is `{what, pattern, effect}`:

    - `what` is the tool's fully-qualified ref (`kolsch/tools/bash`)
      OR a system action namespace (`agent.execute.*`) OR the
      catch-all `*`.
    - `pattern` is a glob matched against the target string (the
      path / command / url the tool receives). Defaults to `*` for
      rules that don't care about the target.
    - `effect` is `allow` / `deny` / `prompt`.

    The legacy shape `{id, who, what, effect}` (with `what` as a
    regex matched against either action or target) is still accepted
    during the in-flight migration — `who` defaults to `None`, which
    the provider's compile step treats as new-shape glob-matched; when
    `who` is supplied (typically `".*"`) the rule compiles as legacy
    regex.

    `id` is derived by
    [`derive_rule_id`][providers.authz.derive_rule_id]: canonical rules
    from `what|pattern|effect` plus `from` when scoped (any supplied
    `id` in YAML is ignored); legacy rules from `who|what|effect` when no `id`
    is supplied. Only the unscoped catch-all deny earns
    [`DEFAULT_DENY_RULE_ID`][providers.authz.DEFAULT_DENY_RULE_ID].
    A legacy rule cannot carry `from_` or a non-`*` `pattern`: the
    regex matcher has no caller or target filter, so either field
    would be silently ignored and the rule would apply more widely
    than it reads. Construction raises (Pydantic `ValidationError`)
    with the
    [`legacy_rule_scope_violation`][schemas.config.legacy_rule_scope_violation]
    text instead.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    what: str
    pattern: str = "*"
    effect: Literal["allow", "deny", "prompt"]

    # Caller-chain `from:` filter. When set, the rule fires only if at
    # least one frame in the `caller_chain` matches `from_`
    # (transitive; empty chain fails closed). Shape validation
    # (3-seg + asset_type in `agents`/`workflows` + catalog existence)
    # happens at rule-compile time inside the provider.
    from_: str | None = Field(default=None, alias="from")

    # Legacy fields — presence of `who` flags the rule as legacy.
    id: str = ""
    who: str | None = None

    # feat-041 — source-location metadata for authz introspection.
    # Populated by config-loaders that route through
    # `_yamlio.load_with_positions`; `None` when unknown (programmatic
    # construction, legacy loaders, `/perm` inserts). Consumers must
    # tolerate `None` and render a `(unknown source)` fallback.
    source_file: str | None = None
    source_line: int | None = None

    @property
    def is_legacy(self) -> bool:
        """Return True when the rule was constructed in the pre-migration shape.

        The compile step keys off this property to choose the regex
        vs. glob matcher. Discriminator is `who`'s presence (rather
        than checking `pattern`'s default) because every legacy rule
        carries `who` and no new-shape YAML rule has one.
        """
        return self.who is not None

    @model_validator(mode="after")
    def _reject_scoped_legacy(self) -> Rule:
        if self.who is not None:
            reason = legacy_rule_scope_violation(
                from_value=self.from_, pattern=self.pattern
            )
            if reason is not None:
                raise ValueError(reason)
        return self


def derive_rule_id(
    who: str,
    what: str,
    effect: str,
    *,
    from_: str | None = None,
) -> str:
    """Return a deterministic id of form `rl_<sha256(...)[:8]>`.

    Stable across reorders and reloads — two rules with identical
    content always produce the same id. The hash input is
    `who|what|effect`; canonical rules pass `who=".*"` and
    `what="<what>|<pattern>"` (see `_ensure_id`). A `from_`-scoped
    rule appends `|from=<from_>`, so a scoped rule and its unscoped
    twin never share an id; with `from_=None` the id is byte-identical
    to the pre-`from_` derivation, so unscoped ids never change.

    The **unscoped** default-deny shape (legacy: `who=".*"`,
    `what=".*"`, `effect="deny"`; canonical: `what="*"`, `pattern="*"`,
    `effect="deny"`, passed in as `what="*|*"`) returns the reserved
    [`DEFAULT_DENY_RULE_ID`][providers.authz.DEFAULT_DENY_RULE_ID]
    instead so the tail is referenced by a fixed name across both
    schema shapes. A scoped catch-all deny never gets that id.

    Legacy rules cannot carry `from` (the provider refuses one that
    does), so only canonical rules pass `from_`.
    """
    if from_ is None:
        if who == ".*" and what == ".*" and effect == "deny":
            return DEFAULT_DENY_RULE_ID
        # Canonical catch-all collapses to
        # `{what: "*", pattern: "*", effect: "deny"}`; `_ensure_id`
        # encodes that as `what="*|*"` here.
        if who == ".*" and what == "*|*" and effect == "deny":
            return DEFAULT_DENY_RULE_ID
        payload = f"{who}|{what}|{effect}"
    else:
        payload = f"{who}|{what}|{effect}|from={from_}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
    return f"rl_{digest}"


@dataclass(frozen=True)
class DefaultDenyTailStatus:
    """Where (if anywhere) a raw rule list carries the default-deny tail.

    Returned by
    [`inspect_default_deny_tail`][providers.authz.inspect_default_deny_tail];
    rendered into user-facing text by
    [`describe_default_deny_tail_problem`][providers.authz.describe_default_deny_tail_problem].
    All indices are 0-based positions in the inspected list
    (non-mapping entries keep their slot, so indices line up with the
    YAML list).

    Attributes:
        state: `"last"` when the first tail is the final entry,
            `"not_last"` when a tail exists but entries follow it (those
            entries can never match), `"missing"` when no entry has the
            tail shape.
        index: Index of the first tail-shaped entry, or `None` when
            `state == "missing"`.
        rule_count: Length of the inspected list.
        catch_all_allow_index: Index of the first unconditional
            catch-all `allow` (the tail's shape with `effect: allow`)
            that precedes the tail — or that appears anywhere, when
            there is no tail. `None` when there is none.
        tail_shape: `"canonical"` (`{what: "*", pattern: "*", effect:
            deny}`) or `"legacy"` (`{who: ".*", what: ".*", effect:
            deny}`) for the first tail; `None` when missing.
    """

    state: Literal["last", "not_last", "missing"]
    index: int | None
    rule_count: int
    catch_all_allow_index: int | None
    tail_shape: Literal["canonical", "legacy"] | None


def _catch_all_shape(
    entry: object, effect: str
) -> Literal["canonical", "legacy"] | None:
    """Return the catch-all shape `entry` has with `effect`, else `None`.

    Legacy: `who == ".*"`, `what == ".*"`. Canonical (no `who`, matching
    [`Rule.is_legacy`][providers.authz.Rule.is_legacy]): `what == "*"`
    with `pattern` absent or `"*"`. Either shape is disqualified by a
    non-`None` `from` / `from_` scope — a scoped catch-all only covers
    one principal, so it is not an unconditional floor.
    """
    if not isinstance(entry, Mapping) or entry.get("effect") != effect:
        return None
    if entry.get("from") is not None or entry.get("from_") is not None:
        return None
    who = entry.get("who")
    if who is not None:
        if who == ".*" and entry.get("what") == ".*":
            return "legacy"
        return None
    if entry.get("what") == "*" and entry.get("pattern", "*") == "*":
        return "canonical"
    return None


def platform_rules_from_config(config: object) -> list[object] | None:
    """Return `authorization.config`'s rule list as the provider enforces it.

    Mirrors the normalization in
    [`RuleBasedAuthorizationProvider`][providers.authz.RuleBasedAuthorizationProvider]:
    an absent or `null` `rules` key is an **empty** rule set (`[]`),
    which has no default-deny tail. Returns `None` when `config` is not
    a mapping or `rules` is not a list after normalization — shapes the
    provider rejects, so there is no enforced rule set to inspect.
    Feed the result to
    [`inspect_default_deny_tail`][providers.authz.inspect_default_deny_tail].

    Args:
        config: The `authorization.config` value (callers pass `{}`
            when the key is absent, as `bootstrap_runtime` does).

    Returns:
        The rule list, or `None` when there is none to inspect.
    """
    if not isinstance(config, Mapping):
        return None
    rules = config.get("rules", []) or []
    return rules if isinstance(rules, list) else None


def inspect_default_deny_tail(rules: Sequence[object]) -> DefaultDenyTailStatus:
    """Classify where `rules` carries the default-deny tail.

    Pure: no I/O, no logging. `rules` is the raw platform rule list as
    parsed from `platform.yaml`'s `authorization.config.rules`
    (mappings; any non-mapping entry is skipped but keeps its index).

    The tail is detected by **shape**, never by id: a legacy rule may
    carry any caller-supplied `id`, so
    [`DEFAULT_DENY_RULE_ID`][providers.authz.DEFAULT_DENY_RULE_ID] alone
    is not proof of a tail. A tail is an
    unscoped catch-all deny in either schema:

    - canonical: `{what: "*", pattern: "*" (or absent), effect: deny}`
      with no `who`;
    - legacy: `{who: ".*", what: ".*", effect: deny}`;

    and in both cases `from` / `from_` absent or `None`.

    Shared by the runtime bootstrap warning and `kolsch doctor`; other
    surfaces that need "is the tail present / where" should call this
    rather than re-deriving it.

    Args:
        rules: The raw rule entries, in evaluation order.

    Returns:
        The [`DefaultDenyTailStatus`][providers.authz.DefaultDenyTailStatus]
        for the list. An empty list is `missing`.
    """
    tail_index: int | None = None
    tail_shape: Literal["canonical", "legacy"] | None = None
    allow_index: int | None = None
    for i, entry in enumerate(rules):
        shape = _catch_all_shape(entry, "deny")
        if shape is not None:
            tail_index, tail_shape = i, shape
            break
        if allow_index is None and _catch_all_shape(entry, "allow") is not None:
            allow_index = i
    count = len(rules)
    if tail_index is None:
        state: Literal["last", "not_last", "missing"] = "missing"
    elif tail_index == count - 1:
        state = "last"
    else:
        state = "not_last"
    return DefaultDenyTailStatus(
        state=state,
        index=tail_index,
        rule_count=count,
        catch_all_allow_index=allow_index,
        tail_shape=tail_shape,
    )


_DEFAULT_DENY_FIX_COMMAND = (
    "kolsch permissions add --what '*' --pattern '*' --effect deny"
)


def describe_default_deny_tail_problem(status: DefaultDenyTailStatus) -> str | None:
    """Render a tail problem as one user-facing paragraph, or `None` if fine.

    Pure. Returns `None` when `status.state == "last"`. Otherwise the
    text says what is wrong, what it means for enforcement today, and
    how to fix it; it always points at `kolsch doctor` for a re-check.
    Positions in the text are 1-based (as a user counts YAML list
    entries). Used verbatim by the runtime bootstrap WARNING and the
    `kolsch doctor` "Authorization" section so both surfaces say the
    same thing.

    Args:
        status: The result of
            [`inspect_default_deny_tail`][providers.authz.inspect_default_deny_tail].

    Returns:
        The problem description, or `None` when the tail is last.
    """
    if status.state == "last":
        return None
    if status.state == "not_last" and status.index is not None:
        after = status.rule_count - status.index - 1
        tail_rule = (
            '{who: ".*", what: ".*", effect: deny}'
            if status.tail_shape == "legacy"
            else '{what: "*", pattern: "*", effect: deny}'
        )
        noun = "rule" if after == 1 else "rules"
        verb = "never matches" if after == 1 else "never match"
        return (
            f"the authorization default-deny tail ({DEFAULT_DENY_RULE_ID}) is at "
            f"position {status.index + 1} of {status.rule_count}, so the {after} "
            f"{noun} after it {verb}. Fix: move the `{tail_rule}` rule to the "
            "end of `authorization.config.rules` in `platform.yaml` by hand "
            "(`kolsch permissions add` would not move it), then run "
            "`kolsch doctor` to re-check."
        )
    head = (
        "the authorization rule set has no default-deny tail "
        f'(`{{what: "*", pattern: "*", effect: deny}}`, id {DEFAULT_DENY_RULE_ID}).'
    )
    if status.catch_all_allow_index is not None:
        return (
            f"{head} Effect: the catch-all allow rule at position "
            f"{status.catch_all_allow_index + 1} allows every action no earlier "
            "rule denies. Fix: remove or narrow that catch-all allow, then run "
            f"`{_DEFAULT_DENY_FIX_COMMAND}` and `kolsch doctor` to re-check."
        )
    return (
        f"{head} Effect: actions no rule covers will ask for approval in "
        "`kolsch chat`, be allowed without asking under "
        "`kolsch chat --auto-approve`, and be denied everywhere else. Fix: run "
        f"`{_DEFAULT_DENY_FIX_COMMAND}`, then `kolsch doctor` to re-check."
    )


def dead_legacy_rule_reason(rule: Rule) -> str | None:
    r"""Return why a legacy rule can never match, or `None`.

    Covers the one precisely decidable case: a legacy `what:` that pins
    the pre-3-segment tool action `tool\.<name>` (the
    `LEGACY_TOOL_ACTION_RE`
    grammar the setup migrator converts). Engine-registered tools act as
    `<bundle>/tools/<name>`, so such a rule is dead — its effect is
    never applied. Pure; callers decide whether to warn. Never consulted
    by `allow()`, so it can't change a decision.
    """
    if not rule.is_legacy:
        return None
    match = LEGACY_TOOL_ACTION_RE.match(rule.what)
    if match is None:
        return None
    name = match.group(1)
    return (
        f"`what: {rule.what!r}` pins the action `tool.{name}`, which no "
        "engine-registered tool uses (tool actions are "
        "`<bundle>/tools/<name>`), so this rule never matches; replace it "
        f'with `{{what: <bundle>/tools/{name}, pattern: "*", '
        f"effect: {rule.effect}}}`"
    )


def warn_dead_legacy_rules(rules: Sequence[Rule], *, where: str) -> int:
    """Log one WARNING per dead legacy rule in `rules`.

    A rule is dead when
    [`dead_legacy_rule_reason`][providers.authz.dead_legacy_rule_reason]
    returns a reason. The text is
    [`ConfigDeprecatedRegexRule`][errors.ConfigDeprecatedRegexRule]'s
    (`config.deprecated_regex_rule`), prefixed with `where` and the
    rule's source location when known. Never raises.

    Args:
        rules: The rules to check, indexed as listed.
        where: Who loaded them (e.g. `"authz.rule_based.load"`,
            `"agent ag_x permissions"`).

    Returns:
        The number of warnings logged.
    """
    warned = 0
    for index, rule in enumerate(rules):
        reason = dead_legacy_rule_reason(rule)
        if reason is None:
            continue
        location = (
            f" ({rule.source_file}:{rule.source_line})"
            if rule.source_file is not None
            else ""
        )
        warning = ConfigDeprecatedRegexRule(
            rule_index=index, what=rule.what, reason=reason, where=where
        )
        _log.warning("%s%s: %s", where, location, warning)
        warned += 1
    return warned


class AuthorizationResult(BaseModel):
    """The structured outcome of one `allow` call.

    Carries the matched rule (or `None` for default-deny) so callers
    can explain the decision. `effect` carries the three-value
    classification; `allowed` collapses `ALLOW` to `True` and both
    `DENY` and `PROMPT` to `False` for callers that only need the
    bool. `rule_id` is a convenience accessor.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    effect: AuthzEffect = AuthzEffect.DENY
    matched_rule: Rule | None = None
    # Always populated — both the matched-rule and default-deny paths
    # in `RuleBasedAuthorizationProvider.allow` set a non-None string.
    # Typed as `str` (not `str | None`) so callers don't have to
    # defend against a None they will never see.
    reason: str

    @property
    def rule_id(self) -> str | None:
        """Return the matched rule's id, or `None` for default-deny."""
        return self.matched_rule.id if self.matched_rule is not None else None


# ---------------------------------------------------------------------------
# PermissionLayer + RuleBasedAuthorizationConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PermissionLayer:
    """One named layer of rules fed to the rule-based authz provider.

    Consumed by
    [`RuleBasedAuthorizationProvider.allow`][providers.authz.RuleBasedAuthorizationProvider.allow].
    The provider walks the `layers` tuple in order — first-match-wins
    per layer. Layer-tuple ordering is the engine's responsibility:
    the workflow layer is included FIRST when the proximate caller is
    a workflow node firing the action; OMITTED entirely when the
    proximate caller is an embedded agent (structural escalation
    prevention with no new rule field required).

    `name` is the layer's display tag (`"workflow"` / `"agent"` /
    `"bundle"` / `"platform"`); it surfaces in the
    `AuthorizationResult.reason` and in the `policy.decision.made`
    event payload's `layer` field.
    """

    name: str
    rules: tuple[Rule, ...]


class RuleBasedAuthorizationConfig(BaseModel):
    """The typed config for `RuleBasedAuthorizationProvider`.

    Existing `platform.yaml` workspaces continue to work via
    `model_validate` against the raw dict.

    `audit_level` (typed as `str`, not `Literal`) gates the engine's
    per-decision audit-event emission:

    - `"rule_matched"` (default): persist only decisions where an
      explicit rule fired.
    - `"all"`: persist every policy decision (compliance-driven
      operators set this in `platform.yaml` / `bundle.yaml`).

    Arbitrary string values are accepted by the model; semantic
    interpretation is the engine's responsibility — unknown values
    collapse to `"rule_matched"` at the emit site.
    """

    model_config = ConfigDict(extra="allow")

    rules: list[dict[str, Any]] = Field(default_factory=list)
    audit_level: str = "rule_matched"


# ---------------------------------------------------------------------------
# Prompt callback
# ---------------------------------------------------------------------------


if TYPE_CHECKING:

    class PromptCallback(Protocol):
        """The hook chat surfaces inject to resolve `prompt` effects.

        Receives the matched rule's id, the action, the optional
        target (resolved path / command string), and the caller_chain
        (proximate-first tuple of caller frames — the modal defaults
        newly-persisted rules' `from:` to `caller_chain[0]` so
        approved rules scope to the current agent by default).
        Returns either:

        - A single [`AuthzEffect`][providers.authz.AuthzEffect]
          (`ALLOW` or `DENY`) — the resolution for this single call;
          no rule is persisted.
        - A tuple `(effect, rule)` — the resolution AND a new rule
          to append to the rule list. The provider inserts the new
          rule via the live-edit API before this call returns so the
          next check in the same exchange sees it.
        """

        async def __call__(
            self,
            *,
            rule_id: str | None,
            action: str,
            target: str | None,
            caller_chain: tuple[str, ...] = (),
            execution_context: ExecutionContext | None = None,
        ) -> AuthzEffect | tuple[AuthzEffect, Rule]:
            """Resolve a `prompt` rule and optionally append a new rule.

            `execution_context` is spec-004 r-review r1's optional
            workflow-context passthrough. Existing call sites pass
            `None`; the workflow executor (spec-011) forwards its
            active `ExecutionContext` so
            `InlineApprovalCallback._derive_source` can tag the
            emitted `kolsch.prompt.requested` event as
            workflow-scoped. Backward-compatible via the default.
            """


@dataclass(frozen=True, slots=True)
class RulePersistRequest:
    """One rule an "always" answer asked to persist (bug-162).

    Handed to the `on_rule_added` hook registered via
    [`register_prompt_callback`][providers.authz.RuleBasedAuthorizationProvider.register_prompt_callback]
    — never the provider's whole in-memory rule list, which is the
    session's startup snapshot and would overwrite rule edits made on
    disk since.

    Attributes:
        rule: The rule just inserted in memory, with its id populated.
        anchor: The **platform-layer** `prompt` rule that asked (the
            persisted rule belongs immediately before its disk copy).
            `None` for the no-rule path and for prompts matched in a
            workflow / bundle / agent layer, whose rule isn't in
            `platform.yaml` — the rule then belongs before the
            default-deny tail.
    """

    rule: Rule
    anchor: Rule | None = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class AuthorizationProvider(Protocol):
    """The two-method contract every authz variant honours.

    `allow` accepts optional `target` and `callback` parameters.
    `target` is matched against the rule's `pattern` (for file tools
    the resolved path; for `bash` the command string). `callback` is
    the chat-surface prompt resolver — when omitted, `prompt`
    collapses to `deny` so unattended callers never hang.
    """

    # PLR0913 acknowledged: this is the unified decision surface —
    # the per-layer kwargs that previously fanned out across multiple
    # methods collapsed into `layers` / `caller_chain` here, and
    # `target` / `callback` are part of the same decision call. A
    # dataclass-shaped request would only shuffle the argument count
    # behind another constructor.
    async def allow(  # noqa: PLR0913
        self,
        principal: str,
        action: str,
        *,
        target: str | None = None,
        callback: PromptCallback | None = None,
        layers: tuple[PermissionLayer, ...] = (),
        caller_chain: tuple[str, ...] = (),
        tool_call_id: str | None = None,
        path_target: PathTarget | None = None,
    ) -> AuthorizationResult:
        """Resolve the authz decision for `(principal, action, target)`.

        `tool_call_id` (feat-041) is threaded through to the
        `policy.decision.made` event payload so `/why <tool_call_id>`
        can correlate deterministically. `None` at call sites that
        aren't per-tool (e.g. `agent.execute.*` pre-flight).

        `path_target` is set by tool dispatch when `target` came from a
        file tool's `path` argument; `target` is then its canonical
        display form. Rule patterns are matched against the target's
        canonical forms — strictly for `allow` rules, generously for
        `deny` / `prompt` rules (see
        [`files.authz_target`][files.authz_target]). `None` (every
        non-path target) keeps raw-string matching.
        """

    async def list_rules(self) -> list[Rule]:
        """Return the provider's full rule list in walk order."""


# ---------------------------------------------------------------------------
# Rule-based variant
# ---------------------------------------------------------------------------


class _CompiledRule:
    """A rule with its match expressions pre-compiled.

    Two shapes coexist during the schema migration window:

    - **Legacy** rules carry `who_re` and `what_re` (compiled regex
      patterns matched via `re.fullmatch`). `what_re` matches against
      the target (preferred) or action.
    - **New-shape** rules carry `what` (glob) and `pattern` (glob),
      both matched via `fnmatch.fnmatchcase`. `what` matches the
      action OR the tool ref; `pattern` matches the target.

    `is_legacy` records which path the matcher should follow. The
    public surface is [`Rule`][providers.authz.Rule] — the raw,
    serialisable record returned through
    [`AuthorizationResult`][providers.authz.AuthorizationResult] and
    `list_rules`.
    """

    __slots__ = (
        "effect",
        "from_pattern",
        "is_legacy",
        "raw",
        "what",
        "what_re",
        "who_re",
    )

    # PLR0913 — the compiled-rule shape carries every match input the
    # matcher needs (effect, raw record, legacy/new-shape regex vs
    # glob pieces, the optional from: caller-chain pattern). All
    # fields are required for one or the other matcher path; pushing
    # them behind a dataclass would only shuffle the arity.
    def __init__(  # noqa: PLR0913
        self,
        effect: Literal["allow", "deny", "prompt"],
        raw: Rule,
        *,
        is_legacy: bool,
        who_re: re.Pattern[str] | None = None,
        what_re: re.Pattern[str] | None = None,
        what: str | None = None,
        from_pattern: str | None = None,
    ) -> None:
        self.effect = effect
        self.raw = raw
        self.is_legacy = is_legacy
        self.who_re = who_re
        self.what_re = what_re
        # The new-shape `what` is matched via fnmatch at evaluation
        # time; pre-compiled translation would help but fnmatch
        # already maintains its own LRU cache for translate().
        self.what = what
        # The validated `from:` glob (or `None` when the rule has no
        # caller-chain filter).
        self.from_pattern = from_pattern


class RuleBasedAuthorizationProvider:
    """The v1 default authz variant — first-match-wins ACL.

    Supports the three-value `effect` (allow / deny / prompt),
    optional callback-driven prompt resolution on `allow`, and the
    live-edit API
    ([`add_rule`][providers.authz.RuleBasedAuthorizationProvider.add_rule],
    [`remove_rule`][providers.authz.RuleBasedAuthorizationProvider.remove_rule],
    [`get_rule`][providers.authz.RuleBasedAuthorizationProvider.get_rule]).
    The default-deny tail, when present, keeps the fixed id
    [`DEFAULT_DENY_RULE_ID`][providers.authz.DEFAULT_DENY_RULE_ID]
    and refuses removal. Construction does not check that the tail
    exists or is last; `bootstrap_runtime` warns about that.
    """

    def __init__(
        self,
        *,
        config: dict[str, Any],
        catalog: AssetCatalog | None = None,
        source_map: RuleSourceMap | None = None,
    ) -> None:
        raw_rules = config.get("rules", []) or []
        if not isinstance(raw_rules, list):
            raise AuthzMalformedRule(
                rule_index=-1,
                error=(f"`rules` must be a list; got {type(raw_rules).__name__}"),
            )
        # The action-to-tool-ref bridge is gone for tools (the action
        # IS the 3-segment ref now; the matcher hits the action
        # directly). The map still carries node entries until the
        # parallel node-namespace unification ships. When `catalog` is
        # `None` (tests, non-workspace callers), the map is empty and
        # the matcher falls back to action-only matching for both
        # kinds.
        self._catalog = catalog
        self._action_to_tool_ref: dict[str, str] = (
            _build_action_to_tool_ref(catalog) if catalog is not None else {}
        )
        # Stored so the engine's tool-dispatch wrapper can read
        # `audit_level` for the `policy.decision.made` event
        # emission. Defaults to `"rule_matched"`.
        self.audit_level: str = str(config.get("audit_level", "rule_matched"))
        # feat-041 — sidecar map keyed by YAML list index carrying
        # (source_file, source_line) tuples for each rule. Empty when
        # the caller loaded rules without position tracking; the
        # compile step degrades gracefully to `source_file=None`.
        self._source_map: RuleSourceMap = source_map or {}
        self._rules: list[_CompiledRule] = [
            self._compile(index, entry) for index, entry in enumerate(raw_rules)
        ]
        # A legacy `tool\.<name>` rule loads but can never match a real
        # tool; say so (warning only). Catalog-less providers are test
        # / non-workspace callers — stay quiet there.
        if catalog is not None:
            warn_dead_legacy_rules(
                [compiled.raw for compiled in self._rules],
                where="authz.rule_based.load",
            )
        # Chat-injected callback resolved by `_resolve_prompt` when
        # `allow(callback=)` is omitted. The chat lifecycle registers
        # on startup and clears on exit so subsequent in-process
        # commands never see a leaked modal surface.
        self._registered_prompt_callback: PromptCallback | None = None
        # Fires after a prompt callback persists a rule via the
        # `(effect, Rule)` tuple return, with ONE `RulePersistRequest`
        # (bug-162: never the in-memory list). The chat wires this to
        # `cli.permissions.mutate_platform_rules`, which inserts that
        # one rule into the rules currently on disk; the modal NEVER
        # writes the YAML directly.
        self._rule_added_hook: (
            Callable[[RulePersistRequest], Awaitable[None] | None] | None
        ) = None

    def _compile(self, index: int, entry: object) -> _CompiledRule:
        """Compile one raw rule entry into a `_CompiledRule`.

        Dispatches on the entry's shape: presence of `who` flags the
        legacy `{id, who, what, effect}` regex shape; absence flags
        the new-shape `{what, pattern, effect}` glob shape.
        """
        if not isinstance(entry, dict):
            raise AuthzMalformedRule(
                rule_index=index,
                error=f"rule must be a mapping; got {type(entry).__name__}",
            )
        # feat-041 — pull source metadata from the sidecar (populated
        # by loaders that route through `_yamlio.load_with_positions`)
        # so the compiled Rule carries `(source_file, source_line)`.
        source = self._source_map.get(index)
        # The discriminator: legacy YAML always carries `who`; new
        # YAML never does. Same heuristic `Rule.is_legacy` uses for
        # in-memory rules.
        if "who" in entry:
            return self._compile_legacy(index, entry, source=source)
        return self._compile_new_shape(index, entry, source=source)

    @staticmethod
    def _compile_legacy(
        index: int,
        entry: dict[str, Any],
        *,
        source: tuple[str, int] | None = None,
    ) -> _CompiledRule:
        """Compile a legacy `{id, who, what, effect}` regex-shape entry.

        The legacy shape has no caller-chain or target filter, so an
        entry carrying `from` (or `from_`) or a non-`*` `pattern` is
        refused with `AuthzMalformedRule` rather than compiled into a
        rule that applies to every caller and target.
        """
        from_value = _entry_from_value(index, entry)
        reason = legacy_rule_scope_violation(
            from_value=from_value,
            pattern=entry.get("pattern", "*"),
            setup_can_migrate=_setup_can_migrate_legacy(entry, from_value),
        )
        if reason is not None:
            raise AuthzMalformedRule(rule_index=index, error=reason)
        for required in ("who", "what", "effect"):
            if required not in entry:
                raise AuthzMalformedRule(
                    rule_index=index,
                    error=f"missing required field {required!r}",
                )
        who = entry["who"]
        what = entry["what"]
        effect = entry["effect"]
        if effect not in ("allow", "deny", "prompt"):
            raise AuthzMalformedRule(
                rule_index=index,
                error=(f"effect must be 'allow', 'deny', or 'prompt'; got {effect!r}"),
            )
        try:
            who_re = re.compile(who)
        except re.error as exc:
            raise AuthzRegexInvalid(
                rule_index=index,
                field="who",
                regex=str(who),
                error=str(exc),
            ) from exc
        try:
            what_re = re.compile(what)
        except re.error as exc:
            raise AuthzRegexInvalid(
                rule_index=index,
                field="what",
                regex=str(what),
                error=str(exc),
            ) from exc
        # Preserve a caller-supplied id if present, otherwise derive
        # deterministically. This lets pre-existing rule lists
        # round-trip stably through `list_rules` + reload.
        supplied_id = entry.get("id")
        rule_id = (
            supplied_id
            if isinstance(supplied_id, str) and supplied_id
            else derive_rule_id(who, what, effect)
        )
        source_file, source_line = (source[0], source[1]) if source else (None, None)
        return _CompiledRule(
            effect=effect,
            raw=Rule(
                id=rule_id,
                who=who,
                what=what,
                effect=effect,
                source_file=source_file,
                source_line=source_line,
            ),
            is_legacy=True,
            who_re=who_re,
            what_re=what_re,
        )

    def _compile_new_shape(
        self,
        index: int,
        entry: dict[str, Any],
        *,
        source: tuple[str, int] | None = None,
    ) -> _CompiledRule:
        for required in ("what", "effect"):
            if required not in entry:
                raise AuthzMalformedRule(
                    rule_index=index,
                    error=f"missing required field {required!r}",
                )
        what = entry["what"]
        pattern = entry.get("pattern", "*")
        effect = entry["effect"]
        if effect not in ("allow", "deny", "prompt"):
            raise AuthzMalformedRule(
                rule_index=index,
                error=(f"effect must be 'allow', 'deny', or 'prompt'; got {effect!r}"),
            )
        if not isinstance(what, str) or not what:
            raise AuthzMalformedRule(
                rule_index=index,
                error=f"`what` must be a non-empty string; got {what!r}",
            )
        if not isinstance(pattern, str):
            raise AuthzMalformedRule(
                rule_index=index,
                error=f"`pattern` must be a string; got {type(pattern).__name__}",
            )
        # Validate `pattern` — must be a glob, not a regex.
        forbidden = [c for c in _PATTERN_REGEX_CHARS if c in pattern]
        if forbidden:
            raise AuthzPatternInvalid(
                rule_index=index,
                pattern=pattern,
                forbidden_chars=forbidden,
            )
        # Validate `what` against the catalog (skipped when no
        # catalog supplied, e.g. tests).
        self._validate_what(index=index, what=what)
        # Strict `from:` validation at rule-compile time. `None` means
        # the rule has no caller-chain filter; otherwise the helper
        # raises a typed Workflow* exception on shape / asset_type /
        # catalog-existence misses. Accept both the "from" YAML alias
        # and the Python "from_" name so programmatic Rule(...)
        # construction works without alias-by.
        from_value = _entry_from_value(index, entry)
        self._validate_from(index=index, from_value=from_value)
        # Derive a stable id so the live-edit API + default-deny tail
        # detection still work for new-shape rules. The unscoped
        # catch-all (`what="*"`, `pattern="*"`, `effect="deny"`)
        # collapses to `DEFAULT_DENY_RULE_ID`; everything else —
        # including a `from`-scoped catch-all — gets a deterministic
        # hash that includes `from`. `who=".*"` is the canonical
        # placeholder fed into `derive_rule_id` so legacy and
        # new-shape catch-alls coalesce to the same id.
        rule_id = derive_rule_id(".*", f"{what}|{pattern}", effect, from_=from_value)
        source_file, source_line = (source[0], source[1]) if source else (None, None)
        return _CompiledRule(
            effect=effect,
            raw=Rule(
                id=rule_id,
                what=what,
                pattern=pattern,
                effect=effect,
                from_=from_value,
                source_file=source_file,
                source_line=source_line,
            ),
            is_legacy=False,
            what=what,
            from_pattern=from_value,
        )

    def _validate_from(self, *, index: int, from_value: str | None) -> None:
        """Strict validation of a rule's `from:` caller-chain filter.

        Three hard-error classes (no warning fallback):

        - non-3-segment shape → `WorkflowFromShapeError`.
        - 3-segment with an asset_type segment outside
          `{"agents", "workflows"}` → `WorkflowFromAssetTypeError`.
        - 3-segment + valid asset_type but the target is absent from
          the catalog → `WorkflowFromNotFound`.

        Glob patterns (`kolsch/agents/*`) skip the catalog-existence
        check — by design they cover an asset set, not a single name.
        Shape (three slash-separated segments + a valid asset_type) is
        still enforced.

        When `self._catalog` is `None` the catalog-existence check is
        skipped (mirrors `_validate_what` — tests construct rule lists
        without a workspace on disk).
        """
        if from_value is None:
            return
        parts = from_value.split("/")
        if len(parts) != _THREE_SEG_PARTS or not all(parts):
            raise WorkflowFromShapeError(
                file="<config>", line=index, from_ref=from_value
            )
        _bundle, segment, name = parts
        if segment not in ("agents", "workflows"):
            raise WorkflowFromAssetTypeError(
                file="<config>",
                line=index,
                from_ref=from_value,
                asset_type=segment,
            )
        # Skip catalog existence for glob patterns AND when no catalog
        # was supplied (test path).
        if self._catalog is None:
            return
        if "*" in name or "?" in name:
            return
        expected_type = "agent" if segment == "agents" else "workflow"
        if self._catalog.lookup(from_value, expected_type=expected_type) is None:
            raise WorkflowFromNotFound(file="<config>", line=index, from_ref=from_value)

    def _validate_what(self, *, index: int, what: str) -> None:
        """Validate a rule's `what:` ref against the catalog.

        - `*` → accept (catch-all).
        - Contains `.` (e.g. `agent.execute.*`) → accept; system-action
          namespace, not catalog-validated.
        - Contains `/` → must resolve to a catalogued asset.
        - Bare name → raise `AuthzBareToolRef` (with candidates) or
          `AssetsAmbiguousShorthand`.

        Skipped entirely when no catalog was supplied at construction
        (test paths construct rule lists without a workspace on disk;
        integration / real-workspace paths always pass a catalog).
        """
        if what == "*":
            return
        if "." in what:
            return  # system action namespace; not catalog-validated
        if self._catalog is None:
            return  # legacy / test path — no catalog to validate against
        if "/" in what:
            # Accept both the legacy 2-segment tool ref
            # (`<bundle>/<name>`; migration shim — do not touch) AND
            # the canonical 3-segment ref for tools / nodes /
            # workflows / agents (`<bundle>/<plural>/<name>`). The
            # 3-segment shape routes through `parse_three_segment`;
            # the plural segment dictates which `expected_type` the
            # catalog lookup uses.
            if is_three_segment(what):
                bundle, segment, name = parse_three_segment(what)
                # Map plural → singular asset_type for catalog lookup.
                segment_to_type = {
                    "tools": "tool",
                    "nodes": "node",
                    "workflows": "workflow",
                    "agents": "agent",
                }
                if segment not in segment_to_type:
                    raise AuthzMalformedRule(
                        rule_index=index,
                        error=(
                            f"`what: {what!r}` is a 3-segment ref but the "
                            f"middle segment {segment!r} is not one of "
                            f"{sorted(segment_to_type)}"
                        ),
                    )
                expected = segment_to_type[segment]
                entry = self._catalog.lookup(what, expected_type=expected)
                if entry is None:
                    raise AuthzToolRefUnknown(
                        rule_index=index,
                        tool_ref=what,
                    )
                return
            # Legacy 2-segment form (migration shim). The grammar
            # promotes the 2-seg ref to the canonical 3-seg via
            # `build_ref` and resolves it against the catalog. The
            # 2-segment form is reserved for tools only — workflows /
            # nodes / agents are 3-seg from day one.
            parts = what.split("/")
            if len(parts) != _TWO_SEG_PARTS or not all(parts):
                raise AuthzMalformedRule(
                    rule_index=index,
                    error=(
                        f"`what: {what!r}` is not a valid 2-segment tool ref "
                        "(expected `<bundle>/<name>`)"
                    ),
                )
            bundle, name = parts
            canonical = build_ref(bundle, "tool", name)
            entry = self._catalog.lookup(canonical, expected_type="tool")
            if entry is None:
                raise AuthzToolRefUnknown(
                    rule_index=index,
                    tool_ref=what,
                )
            return
        # Bare name (no `/`, no `.`, not `*`). Walk the catalog for
        # candidates so the error names the qualified form(s) the
        # user should write. Candidates are 3-segment catalog refs
        # (`<bundle>/tools/<name>`) so the user copies in the
        # canonical form, not the deprecated 2-segment shape.
        candidates = sorted(
            entry.ref
            for entry in self._catalog.assets.values()
            if entry.asset_type == "tool" and entry.ref.rsplit("/", 1)[-1] == what
        )
        if len(candidates) > 1:
            raise AssetsAmbiguousShorthand(
                shorthand=what,
                candidates=candidates,
                asset_type="tool",
            )
        # Single candidate or no candidates — either way the user
        # wrote a bare name and we reject it. With candidates the
        # error lists them; without, the candidates list is empty
        # (still actionable: tells the user the bare form is wrong
        # + nothing matches).
        raise AuthzBareToolRef(
            rule_index=index,
            bare_name=what,
            candidates=candidates,
        )

    # PLR0913 — see Protocol.allow above for the rationale; this
    # implementation mirrors the unified surface.
    async def allow(  # noqa: PLR0913
        self,
        principal: str,
        action: str,
        *,
        target: str | None = None,
        callback: PromptCallback | None = None,
        layers: tuple[PermissionLayer, ...] = (),
        caller_chain: tuple[str, ...] = (),
        tool_call_id: str | None = None,
        path_target: PathTarget | None = None,
    ) -> AuthorizationResult:
        """Evaluate the action and target against the layered rule walk.

        The provider walks `layers` in order — first-match-wins per
        layer; each layer's `name` lands in the result's `reason` and
        in the `policy.decision.made` event payload's `layer` field.

        **Engine-side layer-tuple construction.** The engine builds
        the `layers` tuple with `PermissionLayer(name="workflow",
        rules=...)` FIRST when the proximate caller frame is a
        workflow node firing the action. When the proximate caller is
        an embedded agent (the workflow invoked an `agent` node which
        is now invoking a tool), the `workflow` layer is OMITTED from
        the tuple entirely — structural escalation prevention with no
        new rule field required. Principal strings (`user.local` /
        `system`) stay engine telemetry; they never feed the matcher.

        **Caller chain.** `caller_chain` is the ordered tuple of
        refs the action's call arrived through — proximate first
        (`chain[0]` is closest to the tool call), distal last.
        Default `()` represents engine bootstrap (no agent or
        workflow active). Rules with a `from:` filter fire only
        when ANY frame in the chain matches the glob; an empty
        chain fails closed for any `from:`-filtered rule.

        **Audit emission.** After the rule walk resolves, a
        `policy.decision.made` event is appended to the active
        workflow run's JSONL subject to
        `audit_level`
        (`"all"` emits every decision; `"rule_matched"`, the default,
        emits only when a rule fired). Non-workflow callers (chat,
        `kolsch run`, tests) observe no emission because no
        workflow-run contextvar is active.
        """
        result = await self._compute_decision(
            principal=principal,
            action=action,
            target=target,
            callback=callback,
            layers=layers,
            caller_chain=caller_chain,
            path_target=path_target,
        )
        # Emit `policy.decision.made` into the current workflow run's
        # JSONL when one is active. The contextvar gate means
        # non-workflow callers (chat, CLI run, tests) see zero
        # overhead.
        _maybe_emit_policy_decision(
            audit_level=self.audit_level,
            result=result,
            action=action,
            target=target,
            caller_chain=caller_chain,
            tool_call_id=tool_call_id,
            path_target=path_target,
        )
        return result

    # PLR0913 — preserved from the prior `allow` signature.
    async def _compute_decision(  # noqa: PLR0913
        self,
        *,
        principal: str,
        action: str,
        target: str | None,
        callback: PromptCallback | None,
        layers: tuple[PermissionLayer, ...],
        caller_chain: tuple[str, ...],
        path_target: PathTarget | None = None,
    ) -> AuthorizationResult:
        """Inner rule walk — see `allow` for the full contract."""
        # Walk layers in the order the caller supplied. The engine is
        # responsible for putting the `workflow` layer FIRST when the
        # proximate frame is a workflow node, OMITTING it when the
        # proximate frame is an embedded agent (structural escalation
        # prevention).
        for layer in layers:
            layer_match = await self._match_against_rules(
                rules=[self._compile_rule_record(r) for r in layer.rules],
                principal=principal,
                action=action,
                target=target,
                callback=callback,
                layer=layer.name,
                caller_chain=caller_chain,
                path_target=path_target,
            )
            if layer_match is not None:
                return layer_match

        # Platform rules — same matcher path as the layer rules.
        platform_match = await self._match_against_rules(
            rules=self._rules,
            principal=principal,
            action=action,
            target=target,
            callback=callback,
            layer="platform",
            caller_chain=caller_chain,
            path_target=path_target,
        )
        if platform_match is not None:
            return platform_match

        # No-rule path — fire the registered modal callback if any.
        # Mirrors `_resolve_prompt`'s effective-callback dance: a
        # per-call `callback=` wins, falling back to the chat-injected
        # `_registered_prompt_callback`. When neither is set, fall
        # through to default-deny so non-chat callers (`kolsch run`,
        # server, tests) never hang on a stdin read.
        effective_callback = callback or self._registered_prompt_callback
        if effective_callback is not None:
            return await self._resolve_no_rule(
                callback=effective_callback,
                action=action,
                target=target,
                caller_chain=caller_chain,
            )

        # Default deny.
        return AuthorizationResult(
            allowed=False,
            effect=AuthzEffect.DENY,
            matched_rule=None,
            reason="no rule matched; default deny (who_chain=default)",
        )

    # PLR0913 — internal helper shared by every layer walk; carries
    # the full match context (rule list, principal, action/target,
    # callback, layer tag, caller_chain). Splitting would just move
    # the parameters to an intermediate object.
    async def _match_against_rules(  # noqa: PLR0913
        self,
        *,
        rules: list[_CompiledRule],
        principal: str,
        action: str,
        target: str | None,
        callback: PromptCallback | None,
        layer: str,
        caller_chain: tuple[str, ...] = (),
        path_target: PathTarget | None = None,
    ) -> AuthorizationResult | None:
        """Walk one layer's compiled rules first-match-wins.

        Returns an `AuthorizationResult` on match (PROMPT resolved
        through `_resolve_prompt`); returns `None` when no rule fires
        so the caller can fall through to the next layer.

        `layer` (`"agent"` / `"platform"` / `"workflow"` /
        `"bundle"`) is folded into the result's `reason` (and into
        PROMPT's resolved reason via `_resolve_prompt`) so debugging
        output identifies which layer matched.

        `caller_chain` is threaded into `_rule_matches` so the
        `from:` filter can short-circuit rules whose caller-chain
        glob does not fire.
        """
        # The action-to-tool-ref map is built from the platform
        # catalog at construction; per-layer rules (agent / bundle /
        # workflow) inherit the same map.
        tool_ref = self._action_to_tool_ref.get(action)
        for compiled in rules:
            if not self._rule_matches(
                compiled=compiled,
                principal=principal,
                action=action,
                target=target,
                tool_ref=tool_ref,
                caller_chain=caller_chain,
                path_target=path_target,
            ):
                continue
            effect = AuthzEffect(compiled.effect)
            if effect is AuthzEffect.PROMPT:
                return await self._resolve_prompt(
                    compiled=compiled,
                    action=action,
                    target=target,
                    callback=callback,
                    layer=layer,
                    caller_chain=caller_chain,
                )
            return AuthorizationResult(
                allowed=effect is AuthzEffect.ALLOW,
                effect=effect,
                matched_rule=compiled.raw,
                reason=self._reason_for_match(compiled=compiled, layer=layer),
            )
        return None

    @staticmethod
    def _rule_matches(  # noqa: PLR0913 — spec §3.2 matcher takes the full (principal, action, target, tool_ref) tuple
        *,
        compiled: _CompiledRule,
        principal: str,
        action: str,
        target: str | None,
        tool_ref: str | None,
        caller_chain: tuple[str, ...] = (),
        path_target: PathTarget | None = None,
    ) -> bool:
        """Per-rule match decision; dispatches on shape.

        With `path_target` set, only the target comparisons change:
        they run over the target's canonical forms, picked by the
        rule's effect (strict for `allow`, loose for `deny` /
        `prompt`), with the matching case and Unicode folding. The
        action / `what` / `from:` checks are unchanged, and with
        `path_target=None` the raw `target` string is matched exactly
        as before.

        The FIRST check is the `from:` caller-chain glob: a rule with
        `from_pattern` set fires only when at least one frame of
        `caller_chain` matches the glob (transitive). An empty
        `caller_chain` fails closed for any rule with `from_pattern`
        set — engine bootstrap cannot be `from:`-scoped to an agent
        or workflow that hasn't started yet.
        """
        if compiled.from_pattern is not None:
            if not caller_chain:
                return False
            if not any(
                fnmatch.fnmatchcase(frame, compiled.from_pattern)
                for frame in caller_chain
            ):
                return False
        if compiled.is_legacy:
            # Legacy semantics: re.fullmatch against `who` AND
            # (`what` against target OR action).
            assert compiled.who_re is not None  # set at compile
            assert compiled.what_re is not None
            if not compiled.who_re.fullmatch(principal):
                return False
            if path_target is not None:
                target_match = _legacy_path_target_matches(
                    compiled.what_re,
                    path_target,
                    restrictive=compiled.effect != "allow",
                )
            else:
                target_match = target is not None and bool(
                    compiled.what_re.fullmatch(target)
                )
            action_match = compiled.what_re.fullmatch(action)
            return bool(target_match or action_match)
        # New-shape: `what` glob matches action OR tool_ref; `pattern`
        # glob matches target (or the empty string when target is
        # None — `pattern: "*"` then trivially matches).
        assert compiled.what is not None
        what_pattern = compiled.what
        action_glob = fnmatch.fnmatchcase(action, what_pattern)
        tool_ref_match = tool_ref is not None and fnmatch.fnmatchcase(
            tool_ref, what_pattern
        )
        if not (action_glob or tool_ref_match):
            return False
        return _new_shape_pattern_matches(compiled, target, path_target)

    @staticmethod
    def _reason_for_match(*, compiled: _CompiledRule, layer: str) -> str:
        """Format the `AuthorizationResult.reason` for a matched rule.

        Legacy and new-shape rules carry different identifying fields;
        this helper renders each in its own canonical form so
        downstream log readers can tell them apart.
        """
        if compiled.is_legacy:
            return (
                f"matched rule {compiled.raw.id}: who={compiled.raw.who!r} "
                f"what={compiled.raw.what!r} "
                f"effect={compiled.effect!r} (who_chain={layer})"
            )
        return (
            f"matched rule: what={compiled.raw.what!r} "
            f"pattern={compiled.raw.pattern!r} "
            f"effect={compiled.effect!r} (who_chain={layer})"
        )

    # PLR0913 — same rationale as `_match_against_rules`: the full
    # decision context (compiled rule, action, target, callback,
    # layer, caller_chain) is coherent. Splitting would just move
    # the fields to an intermediate object.
    async def _resolve_prompt(  # noqa: PLR0913
        self,
        *,
        compiled: _CompiledRule,
        action: str,
        target: str | None,
        callback: PromptCallback | None,
        layer: str = "platform",
        caller_chain: tuple[str, ...] = (),
    ) -> AuthorizationResult:
        # When the per-call callback is absent, fall back to the
        # chat-injected registered callback. This keeps the tool
        # dispatcher callback-agnostic: it never has to thread the
        # modal through; it just calls `allow()` and the provider
        # resolves the right callback for the active surface.
        effective_callback = callback or self._registered_prompt_callback
        if effective_callback is None:
            # Non-chat callers (`kolsch run`, server, tests) collapse
            # `prompt` to `deny`. The dispatcher emits
            # `authz.prompt_unattended` at the call site so replay
            # shows what would have been asked.
            return AuthorizationResult(
                allowed=False,
                effect=AuthzEffect.PROMPT,
                matched_rule=compiled.raw,
                reason=(
                    f"prompt rule {compiled.raw.id} matched without "
                    f"a callback; collapsed to deny (who_chain={layer})"
                ),
            )
        answer = await effective_callback(
            rule_id=compiled.raw.id,
            action=action,
            target=target,
            caller_chain=caller_chain,
        )
        if isinstance(answer, tuple):
            resolved_effect, persist_rule = answer
            # Insert the persisted rule before the matched prompt rule
            # so future calls with the same target hit it first
            # ( "always allow/deny" semantics).
            inserted = self._insert_rule_before(persist_rule, anchor_id=compiled.raw.id)
            # Fire the rule-added hook AFTER the in-memory insert. The
            # anchor is only meaningful for a platform-layer rule — a
            # workflow / bundle / agent rule isn't in `platform.yaml`.
            await self._fire_rule_added(
                RulePersistRequest(
                    rule=inserted,
                    anchor=compiled.raw if layer == "platform" else None,
                )
            )
        else:
            resolved_effect = answer
        if resolved_effect not in (AuthzEffect.ALLOW, AuthzEffect.DENY):
            msg = (
                "prompt callback must resolve to ALLOW or DENY; "
                f"got {resolved_effect!r}"
            )
            raise ValueError(msg)
        return AuthorizationResult(
            allowed=resolved_effect is AuthzEffect.ALLOW,
            effect=resolved_effect,
            matched_rule=compiled.raw,
            reason=(
                f"prompt rule {compiled.raw.id} resolved by callback "
                f"to {resolved_effect.value!r} (who_chain={layer})"
            ),
        )

    async def _resolve_no_rule(
        self,
        *,
        callback: PromptCallback,
        action: str,
        target: str | None,
        caller_chain: tuple[str, ...] = (),
    ) -> AuthorizationResult:
        """Resolve the no-rule path via the registered prompt callback.

        Mirrors `_resolve_prompt` but for the case where no rule
        matched — `rule_id` passed to the callback is `None` (the
        modal renders the "no covering rule" variant of the panel).
        On a tuple return the rule lands above the default-deny tail
        (the same spot `kolsch permissions add` with no
        `before`/`after`/`first` uses) and the `_rule_added_hook`
        fires with a `RulePersistRequest(rule, anchor=None)` so the
        chat inserts that one rule before the tail of the rules
        currently in `platform.yaml` (bug-162).
        """
        answer = await callback(
            rule_id=None,
            action=action,
            target=target,
            caller_chain=caller_chain,
        )
        if isinstance(answer, tuple):
            resolved_effect, persist_rule = answer
            inserted = self._insert_at_tail(persist_rule)
            await self._fire_rule_added(RulePersistRequest(rule=inserted))
        else:
            resolved_effect = answer
        if resolved_effect not in (AuthzEffect.ALLOW, AuthzEffect.DENY):
            msg = (
                "no-rule prompt callback must resolve to ALLOW or "
                f"DENY; got {resolved_effect!r}"
            )
            raise ValueError(msg)
        return AuthorizationResult(
            allowed=resolved_effect is AuthzEffect.ALLOW,
            effect=resolved_effect,
            matched_rule=None,
            reason=(
                f"no rule matched; callback resolved to "
                f"{resolved_effect.value!r} (who_chain=no_rule)"
            ),
        )

    async def _fire_rule_added(self, request: RulePersistRequest) -> None:
        """Run the rule-added hook, awaiting it when it returns an awaitable.

        The in-memory insert has already happened and is the
        load-bearing guarantee: a hook failure never undoes it (the
        rule stays live for this session). Failures are logged at
        WARNING, not swallowed silently; surfacing them to the user is
        the hook's job (the chat mounts a notice).
        """
        hook = self._rule_added_hook
        if hook is None:
            return
        try:
            result = hook(request)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 — a hook failure must not fail the decision
            _log.warning(
                "authz: rule-added hook failed; rule %s (what=%r pattern=%r "
                "effect=%s) is live for this session only",
                request.rule.id,
                request.rule.what,
                request.rule.pattern,
                request.rule.effect,
                exc_info=True,
            )

    async def list_rules(self) -> list[Rule]:
        """Return every rule currently held, in evaluation order."""
        return [c.raw for c in self._rules]

    def rules_snapshot(self) -> list[Rule]:
        """Return every rule currently held, in evaluation order (sync).

        Synchronous twin of
        [`list_rules`][providers.authz.RuleBasedAuthorizationProvider.list_rules]
        for callers that run outside an event loop — e.g.
        `cli.permissions.mutate_platform_rules`, which compiles the
        on-disk rule list through a throwaway provider to read each
        entry's normalized fields and stable id.
        """
        return [c.raw for c in self._rules]

    def lookup_tool_ref(self, action: str) -> str | None:
        """Map an action string to a qualified rule-shape ref, when one is needed.

        For tools the action string IS the canonical 3-segment ref
        (`kolsch/tools/bash`), so callers persisting a new rule from
        the modal or `/perm` slash can use `action` directly as the
        rule's `what:`. This method returns `None` for tool actions
        (no bridge entry exists for them anymore).

        It still returns a non-None bridge value for node actions
        (`node.bash` → `kolsch/nodes/bash`) until the parallel
        unification ships for workflow nodes.
        """
        return self._action_to_tool_ref.get(action)

    # -- chat-callback registration ------------------------------------

    def register_prompt_callback(
        self,
        callback: PromptCallback,
        *,
        on_rule_added: (
            Callable[[RulePersistRequest], Awaitable[None] | None] | None
        ) = None,
    ) -> None:
        """Register a chat-injected prompt resolver and rule-persist hook.

        The chat lifecycle calls this once on startup (after runtime
        bootstrap, before the input loop) and pairs it with
        `clear_prompt_callback` in a `finally` so a subsequent
        in-process command never sees the modal surface. See
        `src/cli/chat.py` for the canonical registration.

        Calling `register_prompt_callback` twice replaces the prior
        registration — no implicit chaining. Production chat
        lifecycles only ever register once per process; the replace
        semantics are for test setups that want a fresh modal across
        cases.

        Args:
            callback: The modal callable. Receives keyword args
                `(rule_id, action, target)` and returns either an
                [`AuthzEffect`][providers.authz.AuthzEffect] or a
                tuple `(effect, persisted_rule)`.
            on_rule_added: Optional hook fired AFTER the in-memory
                rule insert (only in the tuple-return path). Receives
                one [`RulePersistRequest`][providers.authz.RulePersistRequest]
                — the inserted rule and the platform prompt rule it
                belongs before (`None` → before the default-deny
                tail) — **never** the provider's rule list (bug-162:
                that list is the session's startup snapshot, and
                writing it back erased rule edits made on disk since).
                May be sync or return an awaitable (awaited before
                `allow` returns, so a slow write can run off the event
                loop). Exceptions are logged at WARNING and never undo
                the in-memory insert. The chat wires this to
                `cli.permissions.mutate_platform_rules`.
        """
        self._registered_prompt_callback = callback
        self._rule_added_hook = on_rule_added

    @property
    def registered_prompt_callback(self) -> PromptCallback | None:
        """The prompt callback registered via `register_prompt_callback`, if any."""
        return self._registered_prompt_callback

    @property
    def rule_added_hook(
        self,
    ) -> Callable[[RulePersistRequest], Awaitable[None] | None] | None:
        """The `on_rule_added` hook registered with the prompt callback, if any."""
        return self._rule_added_hook

    def clear_prompt_callback(self) -> None:
        """Remove the registered modal callback and rule-added hook.

        Called by the chat lifecycle in a `finally` so a subsequent
        in-process `kolsch run` against the same authz provider does
        not see the modal — which would either hang (no modal-stdin
        available) or worse, ALLOW silently. Load-bearing for tests
        that bootstrap then exercise more than one command against
        the same runtime instance.
        """
        self._registered_prompt_callback = None
        self._rule_added_hook = None

    # -- live-edit API -------------------------------------------------

    async def get_rule(self, rule_id: str) -> Rule:
        """Look up one rule by stable id.

        Raises:
            AuthzRuleNotFound: No rule with that id is held.
        """
        from errors import AuthzRuleNotFound  # noqa: PLC0415

        for compiled in self._rules:
            if compiled.raw.id == rule_id:
                return compiled.raw
        raise AuthzRuleNotFound(
            rule_id=rule_id,
            available_ids=[c.raw.id for c in self._rules],
        )

    def _insert_at_tail(self, rule: Rule) -> Rule:
        """Insert `rule` immediately before the default-deny tail.

        Falls back to appending at the very end when no tail is present.
        The no-rule callback path uses this position — it's the same
        spot `kolsch permissions add` picks when no
        `before`/`after`/`first` is supplied — so persisted rules
        participate in subsequent matches in the same
        first-match-wins walk.
        """
        rule = self._ensure_id(rule)
        compiled = self._compile_rule_record(rule)
        self._rules.insert(self._tail_index(), compiled)
        return compiled.raw

    def _insert_rule_before(self, rule: Rule, *, anchor_id: str) -> Rule:
        """Insert `rule` immediately before the rule with `anchor_id`.

        Synchronous — used by the prompt-callback path in
        `_resolve_prompt` where the rule must be live by the time the
        next `allow` call evaluates. Returns the rule with its id
        populated.
        """
        rule = self._ensure_id(rule)
        compiled = self._compile_rule_record(rule)
        for index, existing in enumerate(self._rules):
            if existing.raw.id == anchor_id:
                self._rules.insert(index, compiled)
                return compiled.raw
        # Anchor not found — append before the default-deny tail if
        # present, else at the end.
        self._rules.insert(self._tail_index(), compiled)
        return compiled.raw

    async def add_rule(
        self,
        rule: Rule,
        *,
        before: str | None = None,
        after: str | None = None,
        first: bool = False,
    ) -> Rule:
        """Append or insert a rule into the in-memory rule list.

        Exactly one of `before` / `after` / `first` may be set;
        the default is "before the default-deny tail" (the most
        useful position for an additive allow/deny). The disk write
        of `platform.yaml` is the caller's responsibility — this
        method updates only the in-memory list. The CLI command
        `kolsch permissions add` and the in-chat callback path both
        call this, then rewrite the YAML atomically.

        Raises:
            AuthzRuleInvalid: Multiple position kwargs were set, or
                `before`/`after` named a missing anchor id.
        """
        from errors import AuthzRuleInvalid  # noqa: PLC0415

        position_specs = sum(1 for spec in (before, after, first) if spec)
        if position_specs > 1:
            raise AuthzRuleInvalid(
                position=-1,
                reason=("at most one of `before`, `after`, `first` may be specified"),
            )

        rule = self._ensure_id(rule)
        compiled = self._compile_rule_record(rule)

        if first:
            self._rules.insert(0, compiled)
            return compiled.raw
        if before is not None:
            for index, existing in enumerate(self._rules):
                if existing.raw.id == before:
                    self._rules.insert(index, compiled)
                    return compiled.raw
            raise AuthzRuleInvalid(
                position=-1,
                reason=f"`before` rule id {before!r} not found",
            )
        if after is not None:
            for index, existing in enumerate(self._rules):
                if existing.raw.id == after:
                    self._rules.insert(index + 1, compiled)
                    return compiled.raw
            raise AuthzRuleInvalid(
                position=-1,
                reason=f"`after` rule id {after!r} not found",
            )
        # Default — before the default-deny tail.
        self._rules.insert(self._tail_index(), compiled)
        return compiled.raw

    async def remove_rule(self, rule_id: str) -> None:
        """Remove a rule by id.

        Raises:
            AuthzProtectedRule: `rule_id` is the protected
                [`DEFAULT_DENY_RULE_ID`][providers.authz.DEFAULT_DENY_RULE_ID]
                tail.
            AuthzRuleNotFound: `rule_id` matches no held rule.
        """
        from errors import AuthzProtectedRule, AuthzRuleNotFound  # noqa: PLC0415

        if rule_id == DEFAULT_DENY_RULE_ID:
            raise AuthzProtectedRule(
                rule_id=rule_id,
                reason=(
                    "the default-deny tail is removal-protected; "
                    "add permissive rules before it instead"
                ),
            )
        for index, compiled in enumerate(self._rules):
            if compiled.raw.id == rule_id:
                del self._rules[index]
                return
        raise AuthzRuleNotFound(
            rule_id=rule_id,
            available_ids=[c.raw.id for c in self._rules],
        )

    # -- Helpers --------------------------------------------------------

    def _tail_index(self) -> int:
        """Return the insertion index that lands before the default-deny tail.

        Returns the end of the list when no default-deny tail is present.
        """
        for index, compiled in enumerate(self._rules):
            if compiled.raw.id == DEFAULT_DENY_RULE_ID:
                return index
        return len(self._rules)

    @staticmethod
    def _ensure_id(rule: Rule) -> Rule:
        """Populate `id` on legacy rules; new-shape rules need no id.

        The live-edit API still keys on `id` for the prompt-callback
        rule-insert anchor and the `remove_rule` / `get_rule` paths.
        New-shape rules carry their own identifier shape (the
        matched-rule object itself, surfaced via the denied capsule's
        `what`/`pattern`) but the internal anchor still uses a
        derived id. We synthesise one from `what+pattern+effect`, plus
        `from` when the rule is scoped.
        """
        if rule.id:
            return rule
        if rule.is_legacy:
            assert rule.who is not None
            # No `from_`: a legacy rule can't carry one (bug-159).
            derived = derive_rule_id(rule.who, rule.what, rule.effect)
        else:
            # New-shape: derive from (what, pattern, effect). The
            # `who=".*"` placeholder keeps the derived hash stable
            # across reorders + future who-less callers without
            # leaking the legacy convention into the rule itself.
            derived = derive_rule_id(
                ".*",
                f"{rule.what}|{rule.pattern}",
                rule.effect,
                from_=rule.from_,
            )
        return rule.model_copy(update={"id": derived})

    def _compile_rule_record(self, rule: Rule) -> _CompiledRule:
        """Compile a `Rule` object (vs. a raw YAML dict).

        Dispatches the same way `_compile` does — legacy rules (`who`
        set) use regex; new-shape rules (`who` absent) use glob. Used
        by `add_rule` / `_resolve_prompt` where the caller already
        constructed a `Rule`, and per call for agent / bundle /
        workflow layer rules. A legacy rule carrying `from_` or a
        non-`*` `pattern` raises `AuthzMalformedRule` (`rule_index=-1`)
        before any caller mutates its rule list.
        """
        if rule.is_legacy:
            assert rule.who is not None
            # `Rule`'s validator already refuses this; re-check for
            # `model_construct` / `model_copy` bypasses so no legacy
            # rule with an ignored `from_` / `pattern` ever compiles.
            reason = legacy_rule_scope_violation(
                from_value=rule.from_, pattern=rule.pattern
            )
            if reason is not None:
                raise AuthzMalformedRule(rule_index=-1, error=reason)
            try:
                who_re = re.compile(rule.who)
            except re.error as exc:
                raise AuthzRegexInvalid(
                    rule_index=-1,
                    field="who",
                    regex=rule.who,
                    error=str(exc),
                ) from exc
            try:
                what_re = re.compile(rule.what)
            except re.error as exc:
                raise AuthzRegexInvalid(
                    rule_index=-1,
                    field="what",
                    regex=rule.what,
                    error=str(exc),
                ) from exc
            return _CompiledRule(
                effect=rule.effect,
                raw=rule,
                is_legacy=True,
                who_re=who_re,
                what_re=what_re,
            )
        # New-shape — validate pattern and what; reject regex
        # characters in pattern. Catalog validation is best-effort:
        # callers like agent/bundle permissions construct Rule
        # objects programmatically and we skip catalog checks here
        # (the per-call surface is too hot for repeated catalog
        # walks; the platform-rule path validates at __init__).
        forbidden = [c for c in _PATTERN_REGEX_CHARS if c in rule.pattern]
        if forbidden:
            raise AuthzPatternInvalid(
                rule_index=-1,
                pattern=rule.pattern,
                forbidden_chars=forbidden,
            )
        # Strict `from:` validation for the in-process record path too
        # (the YAML path runs the same check inside
        # `_compile_new_shape`). Programmatic callers can opt out by
        # leaving `rule.from_` unset.
        self._validate_from(index=-1, from_value=rule.from_)
        return _CompiledRule(
            effect=rule.effect,
            raw=rule,
            is_legacy=False,
            what=rule.what,
            from_pattern=rule.from_,
        )


# ---------------------------------------------------------------------------
# Legacy-rule path matching
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=256)
def _ignorecase_twin(pattern: str, flags: int) -> re.Pattern[str]:
    """Return a cached `re.IGNORECASE` recompilation of a legacy regex."""
    return re.compile(pattern, flags | re.IGNORECASE)


def _new_shape_pattern_matches(
    compiled: _CompiledRule,
    target: str | None,
    path_target: PathTarget | None,
) -> bool:
    """Match a new-shape rule's `pattern` glob against the target.

    A path target matches through its canonical forms (strict for
    `allow`, loose for `deny` / `prompt`); any other target is globbed
    raw, with `None` treated as the empty string so `pattern: "*"`
    still matches.
    """
    if path_target is not None:
        return path_pattern_matches(
            compiled.raw.pattern,
            path_target,
            restrictive=compiled.effect != "allow",
        )
    return fnmatch.fnmatchcase(target or "", compiled.raw.pattern)


def _legacy_path_target_matches(
    what_re: re.Pattern[str],
    path_target: PathTarget,
    *,
    restrictive: bool,
) -> bool:
    """Match a legacy `what` regex against a path target's canonical forms.

    Same effect rule as new-shape patterns: `allow` rules see the
    strict forms, `deny` / `prompt` rules the loose forms. When case
    folds for the rule kind the regex runs with `re.IGNORECASE` over
    the folded forms; restrictive rules also try the unfolded forms
    so a regex written with non-NFC text still gets its literal match.
    """
    forms = path_target.comparable_forms(restrictive=restrictive)
    if restrictive:
        forms = (*forms, *path_target.forms_for(restrictive=restrictive))
    regex = (
        _ignorecase_twin(what_re.pattern, what_re.flags)
        if path_target.folds_case(restrictive=restrictive)
        else what_re
    )
    return any(regex.fullmatch(form) for form in forms)


# ---------------------------------------------------------------------------
# policy.decision.made emission
# ---------------------------------------------------------------------------


def _maybe_emit_policy_decision(  # noqa: PLR0913 — single-payload emit gate; six kwargs mirror the payload dict
    *,
    audit_level: str,
    result: AuthorizationResult,
    action: str,
    target: str | None,
    caller_chain: tuple[str, ...],
    tool_call_id: str | None = None,
    path_target: PathTarget | None = None,
) -> None:
    """Append a `policy.decision.made` event to the active workflow run.

    Subject to `audit_level`. The payload carries
    `{action, target, effect, rule_id, caller_chain, tool_call_id,
    source_file, source_line}`. A typed `layer` field is intentionally
    omitted today: the layer name is folded into
    `AuthorizationResult.reason`; hoisting a typed `layer` to the
    result requires a wider schema change tracked separately.

    `tool_call_id` (feat-041) is threaded from
    `ToolContext.tool_call_id` at each caller; `None` for surfaces
    that aren't per-tool (e.g. `agent.execute.*` at
    `engine.py:448`). `source_file` / `source_line` come from the
    matched rule when available.

    When `path_target` is set and its raw spelling differs from the
    canonical `target`, the payload also carries `raw_target` (the
    path as the caller typed it). Canonical inputs add no key, so
    their events are byte-identical to the pre-normalisation shape.

    No-op when no workflow run is active on this async task — chat,
    CLI run, and tests that don't construct a run see zero overhead.
    """
    from workflows.events import (  # noqa: PLC0415 — break import cycle
        PolicyDecisionMadeEvent,
        emit_event,
    )
    from workflows.run_context import (  # noqa: PLC0415
        current_workflow_run_context,
    )

    ctx = current_workflow_run_context()
    if ctx is None:
        return
    normalised = (
        audit_level if audit_level in {"all", "rule_matched"} else ("rule_matched")
    )
    if normalised == "rule_matched" and result.matched_rule is None:
        return
    matched = result.matched_rule
    payload: dict[str, Any] = {
        "action": action,
        "target": target,
        "effect": result.effect.value,
        "rule_id": result.rule_id,
        "caller_chain": list(caller_chain),
        "tool_call_id": tool_call_id,
        "source_file": matched.source_file if matched else None,
        "source_line": matched.source_line if matched else None,
    }
    if path_target is not None and path_target.raw != path_target.display:
        payload["raw_target"] = path_target.raw
    emit_event(
        ctx.store,
        PolicyDecisionMadeEvent(
            workflow_run_id=ctx.run_id,
            trace_id=ctx.trace_id,
            parent_event_id=None,
            payload=payload,
        ),
    )


# ---------------------------------------------------------------------------
# allow_all_test variant — test builds only
# ---------------------------------------------------------------------------


class AllowAllTestAuthorizationProvider:
    """Returns `ALLOW` for everything. Test builds only.

    Constructible ONLY when
    [`KOLSCH_TEST_BUILD_ENV_VAR`][providers.authz.KOLSCH_TEST_BUILD_ENV_VAR]
    is set to `"1"`. Production builds refuse to load it; the
    constructor raises `ConfigInvalid` if the env var is unset.
    """

    def __init__(
        self,
        *,
        config: dict[str, Any],
        catalog: AssetCatalog | None = None,
    ) -> None:
        # `catalog` accepted (and ignored) so the factory protocol
        # stays uniform with `RuleBasedAuthorizationProvider`.
        _ = (config, catalog)
        if os.environ.get(KOLSCH_TEST_BUILD_ENV_VAR) != "1":
            raise ConfigInvalid(
                validation_errors=[
                    {
                        "layer": 2,
                        "source": "providers.authz",
                        "path": "/authorization/variant",
                        "code": "test_only_variant",
                        "message": (
                            "the 'allow_all_test' variant is for tests only "
                            f"and requires {KOLSCH_TEST_BUILD_ENV_VAR}=1 "
                            "in the environment; production builds refuse "
                            "to load it (AGENTS.md hard rule per TOOL-R10)"
                        ),
                    }
                ],
                where="providers.authz.allow_all_test.init",
            )

    async def allow(  # noqa: PLR0913 — Protocol signature; the full authz-check tuple
        self,
        principal: str,
        action: str,
        *,
        target: str | None = None,
        callback: PromptCallback | None = None,
        layers: tuple[PermissionLayer, ...] = (),
        caller_chain: tuple[str, ...] = (),
        tool_call_id: str | None = None,
        path_target: PathTarget | None = None,
    ) -> AuthorizationResult:
        """Return `ALLOW` for every call; all kwargs are ignored."""
        _ = (
            principal,
            action,
            target,
            callback,
            layers,
            caller_chain,
            tool_call_id,
            path_target,
        )
        return AuthorizationResult(
            allowed=True,
            effect=AuthzEffect.ALLOW,
            matched_rule=None,
            reason="allow_all_test variant — test builds only",
        )

    async def list_rules(self) -> list[Rule]:
        """Return an empty list; this variant carries no rules."""
        return []


# ---------------------------------------------------------------------------
# Registry seam
# ---------------------------------------------------------------------------


_REGISTRY: dict[str, Callable[..., AuthorizationProvider]] = {}


def register_authorization_variant(
    name: str,
    factory: Callable[..., AuthorizationProvider],
) -> None:
    """Register a variant factory under `name`."""
    _REGISTRY[name] = factory


def make_authorization_provider(
    *,
    config: dict[str, Any],
    catalog: AssetCatalog | None = None,
) -> AuthorizationProvider:
    """Dispatch by `config['variant']` per `platform.yaml`'s `authorization` block.

    `catalog` forwards to variant factories so the rule-based variant
    can validate qualified tool refs against it. Variants that don't
    care (`allow_all_test`) accept and ignore the kwarg.

    Raises:
        ConfigInvalid: `variant` is unset or names no registered factory.
    """
    variant = config.get("variant")
    body = config.get("config", {}) or {}
    factory = _REGISTRY.get(variant or "")
    if factory is None:
        raise ConfigInvalid(
            validation_errors=[
                {
                    "layer": 2,
                    "source": "providers.authz",
                    "path": "/authorization/variant",
                    "code": "unknown_variant",
                    "message": (
                        f"unknown authorization variant {variant!r}; "
                        f"registered: {sorted(_REGISTRY)}"
                    ),
                }
            ],
            where="providers.authz.make_authorization_provider",
        )
    return factory(config=body, catalog=catalog)


register_authorization_variant("rule_based", RuleBasedAuthorizationProvider)


# Test-only variant. Registered unconditionally — the
# construction-time env-var check in
# `AllowAllTestAuthorizationProvider.__init__` is what enforces the
# production refusal. Registering unconditionally lets
# `make_authorization_provider(variant="allow_all_test")` resolve
# the factory; the factory then validates the env state. No global
# conftest sets `KOLSCH_TEST_BUILD`; tests that need the variant set it
# themselves (e.g. `tests/component/authz/test_phase2_authz.py`). The
# runtime bootstrap never consults `authorization.variant`.
register_authorization_variant("allow_all_test", AllowAllTestAuthorizationProvider)
