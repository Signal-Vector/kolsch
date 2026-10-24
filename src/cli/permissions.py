"""`kolsch permissions {list, add, remove, show}` CLI surface.

The out-of-chat editor for the workspace's authorization rule list.
Routes through the in-memory
[`RuleBasedAuthorizationProvider`][providers.RuleBasedAuthorizationProvider]
runtime API (`list_rules` / `add_rule` / `remove_rule` / `get_rule`)
so the same rule-id scheme, default-deny protection, and atomicity
discipline as the in-chat modal apply here too.

`list` / `show` read through a fresh provider built from disk;
`add` / `remove` go through
[`mutate_platform_rules`][cli.permissions.mutate_platform_rules].

Locked platform rules (`authorization.config.locked_rules`, deny-only,
evaluated before every layer) are managed through the same commands:
`add --locked`, and `list` / `show` / `remove` on `rl_locked_` ids. The
same function writes them, through its locked ops, and never touches
`rules` while doing so.

bug-162: [`mutate_platform_rules`][cli.permissions.mutate_platform_rules]
is the single writer of `authorization.config.rules`. It applies one
insert / upsert / remove to the rules **currently on disk**, under
[`platform_yaml_lock`][assets.platform_config.platform_yaml_lock], and
never writes an in-memory rule list back to the file.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shlex
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TextIO, overload

from assets.platform_config import authorization_rule_source_map, platform_yaml_lock
from errors import (
    AuthzMalformedRule,
    AuthzProtectedRule,
    AuthzRuleInvalid,
    AuthzRuleNotFound,
    AuthzRulesPersistFailed,
    PlatformError,
    WorkspaceNotInitialized,
)
from kolsch._yamlio import (
    YAMLError,
    dump_round_trip_with_layout,
    load,
    load_round_trip_with_layout,
    seq_delete_preserving_comments,
    seq_insert_preserving_comments,
)
from platform_runtime.workspace import workspace_is_initialized
from providers import (
    DEFAULT_DENY_RULE_ID,
    LOCKED_RULE_ID_PREFIX,
    Rule,
    RuleBasedAuthorizationProvider,
    derive_locked_rule_id,
)
from schemas.config import legacy_rule_scope_violation

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from assets.catalog import AssetCatalog
    from errors.authz import RulesPersistFailureReason
    from kolsch._yamlio import RoundTripLayout
    from providers.authz import RulePersistRequest, RuleSourceMap

# ---------------------------------------------------------------------------
# Argparse wiring — called from cli.main.build_parser
# ---------------------------------------------------------------------------


def add_permissions_subparser(
    sub: argparse._SubParsersAction[Any],
) -> argparse.ArgumentParser:
    """Register the `kolsch permissions` subparser tree.

    Four leaf subcommands mirror the runtime API on
    [`RuleBasedAuthorizationProvider`][providers.RuleBasedAuthorizationProvider].
    Returns the parent `permissions` subparser so the dispatcher can
    stash a reference for the help-on-missing-op path.
    """
    perms: argparse.ArgumentParser = sub.add_parser(
        "permissions",
        help="Inspect and edit the workspace authorization rule list.",
    )
    perms_sub = perms.add_subparsers(dest="permissions_command", metavar="<op>")

    perms_sub.add_parser(
        "list",
        help="Print every rule in evaluation order with its stable id.",
    )

    add = perms_sub.add_parser(
        "add",
        help="Insert a new rule into the rule list.",
    )
    add.add_argument(
        "--what",
        required=True,
        help=(
            "Qualified tool ref (e.g. `kolsch/tools/bash`), a system "
            "action namespace (e.g. `agent.execute.*`), or `*`."
        ),
    )
    add.add_argument(
        "--pattern",
        default="*",
        help="Glob matched against the target string. Defaults to `*`.",
    )
    add.add_argument(
        "--from",
        dest="from_",
        default=None,
        metavar="<ref>",
        help=(
            "Optional 3-seg caller-chain glob "
            "(e.g. `kolsch/agents/triage`, `kolsch/workflows/*`). "
            "Scopes the rule to invocations whose caller-chain "
            "contains a matching frame."
        ),
    )
    add.add_argument(
        "--effect",
        required=True,
        choices=("allow", "deny", "prompt"),
        help="Effect classification.",
    )
    add.add_argument(
        "--locked",
        action="store_true",
        help=(
            "Add to `locked_rules` instead: a deny evaluated before every "
            "workflow/agent/bundle layer that no layer allow can override. "
            "Requires `--effect deny`; `--from` and positioning are refused."
        ),
    )
    pos = add.add_mutually_exclusive_group()
    pos.add_argument(
        "--before",
        metavar="<id>",
        help="Insert before the rule with this id.",
    )
    pos.add_argument(
        "--after",
        metavar="<id>",
        help="Insert after the rule with this id.",
    )
    pos.add_argument(
        "--first",
        action="store_true",
        help="Insert at the very top of the rule list.",
    )

    remove = perms_sub.add_parser(
        "remove",
        help="Remove a rule by id (refuses the protected default-deny rule).",
    )
    remove.add_argument("rule_id", help="Stable rule id (e.g. rl_abcdef12).")

    show = perms_sub.add_parser(
        "show",
        help="Print one rule's details.",
    )
    show.add_argument("rule_id", help="Stable rule id.")

    return perms


# ---------------------------------------------------------------------------
# Command handlers — called from cli.main._cmd_permissions
# ---------------------------------------------------------------------------


async def cmd_permissions(
    args: argparse.Namespace,
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    """Dispatch the four leaf ops and return the process exit code."""
    if not workspace_is_initialized(args.root):
        raise WorkspaceNotInitialized(path=str(args.root), env_var_set=False)

    yaml_path = args.root / "platform.yaml"
    config = _read_authz_config(yaml_path)
    # Build a fresh `AssetManagement` so the provider can validate
    # qualified tool refs against the catalog. Initialization is cheap
    # (one workspace scan) and gives every CLI subcommand the same
    # validation surface the chat / engine paths get.
    from assets import AssetManagement  # noqa: PLC0415

    am = AssetManagement(root=args.root)
    await am.initialize()
    provider = RuleBasedAuthorizationProvider(
        config=config,
        catalog=am.catalog(),
        config_file=str(yaml_path),
    )

    op = args.permissions_command
    if op == "list":
        return await _cmd_list(provider, out=out)
    if op == "show":
        return await _cmd_show(provider, args.rule_id, out=out, err=err)
    if op == "add":
        return _cmd_add(
            args, yaml_path=yaml_path, catalog=am.catalog(), out=out, err=err
        )
    if op == "remove":
        return _cmd_remove(
            args.rule_id, yaml_path=yaml_path, catalog=am.catalog(), out=out, err=err
        )
    err.write(f"unknown permissions op {op!r}; expected one of list/add/remove/show\n")
    return 1


async def _cmd_list(provider: RuleBasedAuthorizationProvider, *, out: TextIO) -> int:
    """List every rule, marking legacy-shape entries.

    Legacy regex rules render with a `[legacy regex — please convert]`
    marker so users know which entries still need the schema
    migration. New-shape rules render in the canonical
    `what / pattern / effect` form.
    """
    locked = await provider.list_locked_rules()
    if locked:
        # Only when locked rules exist, so the output of a workspace
        # without them is unchanged.
        out.write(_LOCKED_SECTION_HEADER + "\n")
        locked_id_w = max(len(r.id) for r in locked)
        for rule in locked:
            out.write(
                f"{rule.id:<{locked_id_w}}  {rule.effect}  what={rule.what!r}  "
                f"pattern={rule.pattern!r}  (locked)\n"
            )
        out.write("\nrules:\n")
    rules = await provider.list_rules()
    if not rules:
        out.write("(no rules)\n")
        return 0
    # Column widths for readability.
    id_w = max(len(r.id) for r in rules)
    effect_w = max(len(r.effect) for r in rules)
    for rule in rules:
        protected = "  (protected)" if rule.id == DEFAULT_DENY_RULE_ID else ""
        if rule.who is None:
            # New shape — surface what/pattern (and from: when set).
            from_str = f"  from={rule.from_!r}" if rule.from_ is not None else ""
            out.write(
                f"{rule.id:<{id_w}}  {rule.effect:<{effect_w}}  "
                f"what={rule.what!r}  pattern={rule.pattern!r}"
                f"{from_str}{protected}\n"
            )
        else:
            out.write(
                f"{rule.id:<{id_w}}  {rule.effect:<{effect_w}}  "
                f"who={rule.who!r}  what={rule.what!r}  "
                f"[legacy regex — please convert]{protected}\n"
            )
    return 0


async def _cmd_show(
    provider: RuleBasedAuthorizationProvider,
    rule_id: str,
    *,
    out: TextIO,
    err: TextIO,
) -> int:
    try:
        rule = await provider.get_rule(rule_id)
    except AuthzRuleNotFound as exc:
        err.write(f"error: {exc}\n")
        return 1
    out.write(f"id:     {rule.id}\n")
    if rule.who is None:
        out.write(f"what:    {rule.what!r}\n")
        out.write(f"pattern: {rule.pattern!r}\n")
        if rule.from_ is not None:
            out.write(f"from:    {rule.from_!r}\n")
    else:
        out.write(f"who:    {rule.who!r}\n")
        out.write(f"what:   {rule.what!r}\n")
        out.write("        (legacy regex shape — please convert)\n")
    out.write(f"effect: {rule.effect}\n")
    if rule.id.startswith(LOCKED_RULE_ID_PREFIX):
        out.write(
            "        (locked — evaluated before every layer; cannot be overridden)\n"
        )
    if rule.id == DEFAULT_DENY_RULE_ID:
        out.write("        (protected — removal refused)\n")
    return 0


def _cmd_add(
    args: argparse.Namespace,
    *,
    yaml_path: Path,
    catalog: AssetCatalog,
    out: TextIO,
    err: TextIO,
) -> int:
    """Insert one rule into the rules currently on disk (bug-162).

    Positions as before: `--first`, `--before <id>`, `--after <id>`,
    else immediately before the default-deny tail. An identical rule
    already in effect at or before that slot is reported, not
    duplicated. `--locked` appends to `locked_rules` instead.
    """
    if getattr(args, "locked", False):
        return _cmd_add_locked(
            args, yaml_path=yaml_path, catalog=catalog, out=out, err=err
        )
    new_rule = Rule(
        what=args.what,
        pattern=args.pattern,
        from_=args.from_,
        effect=args.effect,
    )
    position: InsertPosition = "tail"
    ref_id: str | None = None
    if args.before:
        position, ref_id = "before", args.before
    elif args.after:
        position, ref_id = "after", args.after
    elif args.first:
        position = "first"
    try:
        result = mutate_platform_rules(
            yaml_path,
            InsertRule(new_rule, position=position, ref_id=ref_id),
            catalog=catalog,
        )
    except PlatformError as exc:  # invalid op / rule, or not persisted
        err.write(f"error: {exc}\n")
        return 1
    rule = result.rule or new_rule
    from_str = f" from={rule.from_!r}" if rule.from_ is not None else ""
    described = (
        f"{rule.id}: what={rule.what!r} pattern={rule.pattern!r}"
        f"{from_str} effect={rule.effect}"
    )
    if not result.inserted:
        out.write(
            f"rule {described} already present at position {result.position}; "
            "nothing written\n"
        )
        return 0
    out.write(f"added rule {described} (position {result.position})\n")
    return 0


def _cmd_add_locked(
    args: argparse.Namespace,
    *,
    yaml_path: Path,
    catalog: AssetCatalog,
    out: TextIO,
    err: TextIO,
) -> int:
    """`add --locked`: check the flags, append to the on-disk `locked_rules`."""
    if args.effect != "deny":
        err.write(
            f"error: --locked requires --effect deny (got {args.effect}); "
            "locked rules are deny-only\n"
        )
        return 1
    if args.from_ is not None:
        err.write(
            "error: --locked does not accept --from; locked rules apply "
            "to every caller\n"
        )
        return 1
    if args.before or args.after or args.first:
        err.write(
            "error: --locked does not accept --before/--after/--first; "
            "locked rules are appended to `locked_rules`\n"
        )
        return 1
    try:
        result = mutate_platform_rules(
            yaml_path,
            InsertLockedRule(Rule(what=args.what, pattern=args.pattern, effect="deny")),
            catalog=catalog,
        )
    except PlatformError as exc:  # invalid rule, duplicate, or not persisted
        err.write(f"error: {exc}\n")
        return 1
    added = result.rule
    out.write(
        f"added locked rule {added.id}: what={added.what!r} "
        f"pattern={added.pattern!r} effect={added.effect}\n"
    )
    return 0


def _cmd_remove(
    rule_id: str,
    *,
    yaml_path: Path,
    catalog: AssetCatalog,
    out: TextIO,
    err: TextIO,
) -> int:
    """Remove the first rule with `rule_id` from the rules on disk (bug-162).

    An `rl_locked_` id is removed from the on-disk `locked_rules` instead.
    """
    if rule_id.startswith(LOCKED_RULE_ID_PREFIX):
        try:
            locked = mutate_platform_rules(
                yaml_path, RemoveLockedRule(rule_id=rule_id), catalog=catalog
            )
        except (AuthzRuleNotFound, AuthzRulesPersistFailed) as exc:
            err.write(f"error: {exc}\n")
            return 1
        out.write(f"removed rule {rule_id} (was locked position {locked.position})\n")
        return 0
    try:
        result = mutate_platform_rules(
            yaml_path, RemoveRule(rule_id=rule_id), catalog=catalog
        )
    except (AuthzProtectedRule, AuthzRuleNotFound, AuthzRulesPersistFailed) as exc:
        err.write(f"error: {exc}\n")
        return 1
    out.write(f"removed rule {rule_id} (was position {result.position})\n")
    return 0


# ---------------------------------------------------------------------------
# platform.yaml round-trip
# ---------------------------------------------------------------------------


def _read_authz_config(yaml_path: Path) -> dict[str, Any]:
    """Read the ``authorization.config`` section from platform.yaml.

    Returns an empty rules list when the section is absent so the
    provider constructs cleanly (the user adding their first rule
    sees a working CLI rather than a load-time failure).
    """
    if not yaml_path.is_file():
        return {"rules": []}
    payload = load(yaml_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        return {"rules": []}
    authz = payload.get("authorization") or {}
    if not isinstance(authz, dict):
        return {"rules": []}
    cfg = authz.get("config") or {}
    if not isinstance(cfg, dict):
        return {"rules": []}
    return cfg


def _rule_to_dict(rule: Rule) -> dict[str, str]:
    """Serialize a Rule for round-trip.

    New-shape rules (`who is None`) serialize without `id` / `who` and
    carry `pattern` (plus `from` when scoped); legacy rules still
    serialize with the old `{id, who, what, effect}` shape so an
    unmigrated workspace that gets one rule added/removed via the
    CLI doesn't accidentally flip back to the new shape mid-file.

    Raises:
        AuthzMalformedRule: A legacy rule carries `from_` or a non-`*`
            `pattern` (only reachable through a validation bypass);
            writing it would drop the field and widen the rule.
    """
    if rule.who is None:
        out: dict[str, str] = {
            "what": rule.what,
            "pattern": rule.pattern,
        }
        if rule.from_ is not None:
            out["from"] = rule.from_
        out["effect"] = rule.effect
        return out
    # The legacy shape can't carry `from` / `pattern`. `Rule` refuses
    # one that does, but a `model_construct` bypass could still reach
    # here — refuse rather than write a widened rule to disk.
    reason = legacy_rule_scope_violation(from_value=rule.from_, pattern=rule.pattern)
    if reason is not None:
        raise AuthzMalformedRule(rule_index=-1, error=reason)
    return {
        "id": rule.id,
        "who": rule.who,
        "what": rule.what,
        "effect": rule.effect,
    }


# ---------------------------------------------------------------------------
# bug-162 — locked, insert-only mutation of the on-disk rule list
# ---------------------------------------------------------------------------

InsertPosition = Literal["anchor_or_tail", "tail", "first", "before", "after"]


@dataclass(frozen=True, slots=True)
class InsertRule:
    """Insert one rule into the on-disk rule list.

    `position` picks the slot, computed on the **disk** list:

    - `anchor_or_tail` — immediately before the first disk rule
      structurally equal to `anchor` (the prompt rule that asked);
      when `anchor` is `None` or not on disk, as `tail`.
    - `tail` — immediately before the default-deny tail (found by
      shape), else at the end.
    - `first` — at index 0.
    - `before` / `after` — next to the first disk rule whose id is
      `ref_id`; a missing id raises `AuthzRuleInvalid`, as
      [`add_rule`][providers.authz.RuleBasedAuthorizationProvider.add_rule]
      does.

    An identical rule already on disk at or before that slot makes the
    insert redundant (first match wins): nothing is written (D7).
    """

    rule: Rule
    anchor: Rule | None = None
    position: InsertPosition = "anchor_or_tail"
    ref_id: str | None = None


@dataclass(frozen=True, slots=True)
class UpsertRule:
    """`/perm allow|deny|prompt` semantics on the disk list.

    Drops the first new-shape disk rule with the same
    `(what, pattern, from)` (any effect), then inserts `rule` before
    the default-deny tail (found by shape), else at the end.
    """

    rule: Rule


@dataclass(frozen=True, slots=True)
class RemoveRule:
    """Remove one rule from the disk list.

    Exactly one of:

    - `match` — the first **new-shape** disk rule with the same
      `(what, pattern, from)` as `match` (any effect; the `/perm
      remove` semantics). No match is not an error: nothing is
      written and `removed` is `False`.
    - `rule_id` — the first disk rule with that stable id (the
      `kolsch permissions remove` semantics). An unknown id raises
      `AuthzRuleNotFound`.

    The default-deny tail is removal-protected either way
    (`AuthzProtectedRule`).
    """

    match: Rule | None = None
    rule_id: str | None = None


RulesOp = InsertRule | UpsertRule | RemoveRule


@dataclass(frozen=True, slots=True)
class InsertLockedRule:
    """Append one locked platform rule to the on-disk `locked_rules`.

    Validated exactly as `locked_rules` is at load (deny-only,
    `{what, pattern, effect}`, three-segment `what`, not the catch-all);
    a rule already locked raises `AuthzRuleInvalid`. Creates the key on
    the first insert. Never touches `rules`.
    """

    rule: Rule


@dataclass(frozen=True, slots=True)
class RemoveLockedRule:
    """Remove the locked rule with `rule_id` from the on-disk `locked_rules`.

    An unknown id raises `AuthzRuleNotFound`; the key is dropped when
    the list empties. Locked rules are not removal-protected. Never
    touches `rules`.
    """

    rule_id: str


LockedRulesOp = InsertLockedRule | RemoveLockedRule


@dataclass(frozen=True, slots=True)
class LockedRulesMutationResult:
    """What a locked op of `mutate_platform_rules` did.

    Attributes:
        written: `platform.yaml` was rewritten (always, on success).
        position: 0-based index of the inserted rule (in
            `disk_locked_after`) or of the removed one (in
            `disk_locked_before`).
        rule: The inserted or removed locked rule, id populated.
        disk_locked_before: The disk `locked_rules` as read under the lock.
        disk_locked_after: The disk `locked_rules` after the op.
    """

    written: bool
    position: int
    rule: Rule
    disk_locked_before: list[Rule]
    disk_locked_after: list[Rule]


@dataclass(frozen=True, slots=True)
class RulesMutationResult:
    """What [`mutate_platform_rules`][cli.permissions.mutate_platform_rules] did.

    Attributes:
        written: `platform.yaml` was rewritten. `False` when the op was
            a no-op (identical rule already in effect, nothing matched
            for removal, or the list would not change).
        inserted: A rule entry was added.
        removed: A rule entry was dropped (the upsert's replaced rule
            counts).
        position: 0-based disk index of the inserted rule (in
            `disk_rules_after`), of the identical rule already present
            (D7 no-op, in `disk_rules_before`), or of the removed rule
            (in `disk_rules_before`). `None` when nothing matched.
        replaced_position: For an upsert, the 0-based index in
            `disk_rules_before` of the rule it dropped; else `None`.
        rule: The inserted / already-present / removed rule, with its
            id populated; `None` when nothing matched.
        disk_rules_before: The disk rules as read under the lock.
        disk_rules_after: The disk rules after the op (equal to
            `disk_rules_before` when nothing was written).
        source_map: `authorization_rule_source_map` of the file after
            the op (feat-041 `/why` breadcrumbs).
    """

    written: bool
    inserted: bool
    removed: bool
    position: int | None
    replaced_position: int | None
    rule: Rule | None
    disk_rules_before: list[Rule]
    disk_rules_after: list[Rule]
    source_map: RuleSourceMap


@overload
def mutate_platform_rules(
    yaml_path: Path,
    op: RulesOp,
    *,
    catalog: AssetCatalog | None,
    lock_timeout_s: float = 5.0,
) -> RulesMutationResult: ...


@overload
def mutate_platform_rules(
    yaml_path: Path,
    op: LockedRulesOp,
    *,
    catalog: AssetCatalog | None,
    lock_timeout_s: float = 5.0,
) -> LockedRulesMutationResult: ...


def mutate_platform_rules(
    yaml_path: Path,
    op: RulesOp | LockedRulesOp,
    *,
    catalog: AssetCatalog | None,
    lock_timeout_s: float = 5.0,
) -> RulesMutationResult | LockedRulesMutationResult:
    """Apply one rule op to the rules **currently on disk** in `yaml_path`.

    The only writer of `authorization.config.rules` (bug-162). Under
    [`platform_yaml_lock`][assets.platform_config.platform_yaml_lock]
    it reads the file, validates the disk rules, inserts or removes
    exactly one entry of the round-trip-loaded list (every comment,
    other entry and other key is kept), and replaces the file
    atomically (temp file + fsync + `os.replace`). It never writes a
    caller's in-memory rule list, so edits made on disk by anyone else
    survive.

    Anchors and the default-deny tail are found **structurally** on the
    disk list, never by derived id: `derive_rule_id` ignores `from:`,
    so ids can't tell a scoped catch-all deny from the tail.

    On any failure the file is left byte-identical and a missing file
    is never created. A file with no `authorization` section (or a
    `null` `config` / `rules`) gets the section created, as the CLI
    always has.

    Args:
        yaml_path: The workspace's `platform.yaml`. The lock lives at
            `yaml_path.parent / ".platform" / "platform.yaml.lock"`.
        op: The [`InsertRule`][cli.permissions.InsertRule],
            [`UpsertRule`][cli.permissions.UpsertRule] or
            [`RemoveRule`][cli.permissions.RemoveRule] to apply.
        catalog: Validates qualified `what` refs and `from:` targets,
            as the runtime provider does; `None` skips that check.
        lock_timeout_s: Bounded wait for the lock.

    Returns:
        A [`RulesMutationResult`][cli.permissions.RulesMutationResult].

    Raises:
        AuthzRulesPersistFailed: `missing` / `unreadable` /
            `unparseable` / `invalid_rules` / `lock_timeout` /
            `write_failed` — nothing was written.
        AuthzProtectedRule: The op would remove the default-deny tail.
        AuthzRuleNotFound: `RemoveRule(rule_id=)` names no disk rule.
        AuthzRuleInvalid: A malformed op, `before` / `after` names no
            disk rule, or the new rule fails to compile with a
            non-`PlatformError` (e.g. a malformed `from:`).
        PlatformError: The new rule itself fails to compile (e.g.
            `authz.pattern_invalid`); raised as the provider raises it.

    The locked ops ([`InsertLockedRule`][cli.permissions.InsertLockedRule],
    [`RemoveLockedRule`][cli.permissions.RemoveLockedRule]) take the same
    lock and atomic write but edit only `authorization.config.locked_rules`
    and return a
    [`LockedRulesMutationResult`][cli.permissions.LockedRulesMutationResult].
    """
    if isinstance(op, InsertLockedRule | RemoveLockedRule):
        return _mutate_locked_rules(
            yaml_path, op, catalog=catalog, lock_timeout_s=lock_timeout_s
        )
    _check_op(op)
    if not yaml_path.is_file():
        # Checked before the lock too, so a missing workspace file never
        # gets a `.platform/` lock dir created next to it.
        raise _persist_failed(
            yaml_path, "missing", "platform.yaml does not exist; not creating it"
        )
    if op_rule := _op_new_rule(op):
        # Compile the new rule on its own first so a bad rule surfaces
        # as the provider's own error, not as "the disk is invalid".
        try:
            RuleBasedAuthorizationProvider(
                config={"rules": [_rule_to_dict(op_rule)]}, catalog=catalog
            )
        except PlatformError:
            raise
        except Exception as exc:  # e.g. a malformed `from:` (WorkflowFrom*)
            raise AuthzRuleInvalid(
                position=-1,
                reason=f"the new rule does not compile: {exc}",
                where="cli.permissions.mutate_platform_rules",
            ) from exc
    with platform_yaml_lock(yaml_path.parent, timeout_s=lock_timeout_s):
        document, layout, entries = _load_disk_rules(yaml_path)
        before = _compile_disk_rules(yaml_path, _plain(entries), catalog)
        plan = _plan_mutation(op, before, _plain(entries))
        if plan.remove_at is not None:
            seq_delete_preserving_comments(entries, plan.remove_at)
        if plan.insert_at is not None and plan.insert_rule is not None:
            if not entries:
                # `rules: []` loads as a flow-style seq; keep new
                # entries in block style like the rest of the file.
                with contextlib.suppress(AttributeError):
                    entries.fa.set_block_style()
            seq_insert_preserving_comments(
                entries, plan.insert_at, _rule_to_dict(plan.insert_rule)
            )
        after = _compile_disk_rules(yaml_path, _plain(entries), catalog)
        written = [_rule_key(r) for r in after] != [_rule_key(r) for r in before]
        if written:
            _write_document(yaml_path, document, layout)
        else:
            after = before
        source_map = authorization_rule_source_map(yaml_path.parent)
    inserted = written and plan.insert_at is not None
    removed = written and plan.remove_at is not None
    if inserted and plan.insert_at is not None:
        position: int | None = plan.insert_at
        rule: Rule | None = after[plan.insert_at]
    else:
        position = plan.report_at
        rule = before[plan.report_at] if plan.report_at is not None else None
    return RulesMutationResult(
        written=written,
        inserted=inserted,
        removed=removed,
        position=position,
        replaced_position=plan.remove_at if isinstance(op, UpsertRule) else None,
        rule=rule,
        disk_rules_before=before,
        disk_rules_after=after,
        source_map=source_map,
    )


@dataclass(frozen=True, slots=True)
class _MutationPlan:
    """Indices (into the disk list as read) one op resolves to."""

    insert_at: int | None = None
    insert_rule: Rule | None = None
    # Applied BEFORE the insert; `insert_at` is already adjusted for it.
    remove_at: int | None = None
    # Reported when nothing is inserted: the D7 duplicate or the removed rule.
    report_at: int | None = None


def _check_op(op: RulesOp) -> None:
    """Reject malformed ops and protected removals before any I/O."""
    if isinstance(op, InsertRule):
        if op.position in ("before", "after") and not op.ref_id:
            raise AuthzRuleInvalid(
                position=-1,
                reason=f"`{op.position}` needs a rule id",
            )
        if op.position not in ("before", "after") and op.ref_id is not None:
            raise AuthzRuleInvalid(
                position=-1,
                reason=f"a rule id is only valid with `before` / `after`, "
                f"not {op.position!r}",
            )
    elif isinstance(op, UpsertRule):
        if op.rule.who is not None:
            raise AuthzRuleInvalid(
                position=-1,
                reason="upsert takes a new-shape rule (no `who`)",
            )
    elif (op.match is None) == (op.rule_id is None):
        raise AuthzRuleInvalid(
            position=-1,
            reason="remove takes exactly one of `match` / `rule_id`",
        )
    elif op.rule_id == DEFAULT_DENY_RULE_ID:
        raise _protected(DEFAULT_DENY_RULE_ID)


def _op_new_rule(op: RulesOp) -> Rule | None:
    if isinstance(op, InsertRule | UpsertRule):
        return op.rule
    return None


def _protected(rule_id: str) -> AuthzProtectedRule:
    return AuthzProtectedRule(
        rule_id=rule_id,
        reason=(
            "the default-deny tail is removal-protected; "
            "add permissive rules before it instead"
        ),
        where="cli.permissions.mutate_platform_rules",
    )


def _persist_failed(
    yaml_path: Path, reason: RulesPersistFailureReason, detail: str
) -> AuthzRulesPersistFailed:
    return AuthzRulesPersistFailed(path=str(yaml_path), reason=reason, detail=detail)


def _load_disk_rules(yaml_path: Path) -> tuple[Any, RoundTripLayout, Any]:
    """Round-trip-load `yaml_path`; return `(document, layout, rules_seq)`.

    Creates an absent `authorization` / `config` / `rules` node in the
    loaded document (written only if the op then changes the list).
    """
    document, layout, config = _load_disk_config(yaml_path)
    rules = config.get("rules")
    if rules is None:
        config["rules"] = []
        rules = config["rules"]
    if not isinstance(rules, list):
        raise _persist_failed(
            yaml_path,
            "unparseable",
            f"`authorization.config.rules` must be a list; got {type(rules).__name__}",
        )
    return document, layout, rules


def _load_disk_config(yaml_path: Path) -> tuple[Any, RoundTripLayout, Any]:
    """Round-trip-load `yaml_path`; return `(document, layout, authorization.config)`.

    Creates an absent `authorization` / `config` node in the loaded
    document (written only if the op then changes it).
    """
    if not yaml_path.is_file():
        raise _persist_failed(
            yaml_path, "missing", "platform.yaml does not exist; not creating it"
        )
    try:
        text = yaml_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _persist_failed(yaml_path, "unreadable", str(exc)) from exc
    except UnicodeDecodeError as exc:
        raise _persist_failed(yaml_path, "unparseable", f"not UTF-8: {exc}") from exc
    try:
        document, layout = load_round_trip_with_layout(text)
    except YAMLError as exc:
        raise _persist_failed(yaml_path, "unparseable", str(exc)) from exc
    if document is None:
        raise _persist_failed(
            yaml_path, "unparseable", "the file is empty (no top-level mapping)"
        )
    if not isinstance(document, dict):
        raise _persist_failed(
            yaml_path,
            "unparseable",
            f"top level must be a mapping; got {type(document).__name__}",
        )
    authz = document.get("authorization")
    if authz is None:
        document["authorization"] = {"variant": "rule_based", "config": {"rules": []}}
        authz = document["authorization"]
    if not isinstance(authz, dict):
        raise _persist_failed(
            yaml_path,
            "unparseable",
            f"`authorization` must be a mapping; got {type(authz).__name__}",
        )
    config = authz.get("config")
    if config is None:
        authz["config"] = {"rules": []}
        config = authz["config"]
    if not isinstance(config, dict):
        raise _persist_failed(
            yaml_path,
            "unparseable",
            f"`authorization.config` must be a mapping; got {type(config).__name__}",
        )
    return document, layout, config


def _plain(value: Any) -> Any:
    """Copy a round-trip YAML node into plain `dict` / `list` / `str` values."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, str):
        return str(value)
    return value


def _compile_disk_rules(
    yaml_path: Path, entries: list[Any], catalog: AssetCatalog | None
) -> list[Rule]:
    """Compile `entries` as the runtime would; refuse to write over invalid rules."""
    try:
        provider = RuleBasedAuthorizationProvider(
            config={"rules": entries}, catalog=catalog
        )
    except Exception as exc:  # any compile failure — D4: refuse to write
        raise _persist_failed(yaml_path, "invalid_rules", str(exc)) from exc
    return provider.rules_snapshot()


def _mutate_locked_rules(
    yaml_path: Path,
    op: LockedRulesOp,
    *,
    catalog: AssetCatalog | None,
    lock_timeout_s: float,
) -> LockedRulesMutationResult:
    """Apply one locked op to the on-disk `locked_rules`, under the lock.

    Validation is the provider's own load-time check, run over the disk
    list plus the new entry, so the CLI can't write a lock the runtime
    would refuse.
    """
    if not yaml_path.is_file():
        raise _persist_failed(
            yaml_path, "missing", "platform.yaml does not exist; not creating it"
        )
    if isinstance(op, InsertLockedRule):
        # The new rule on its own first, so its own problem (deny-only,
        # 3-segment `what`, ...) surfaces as the provider raises it.
        problem = _locked_rule_compile_problem([_rule_to_dict(op.rule)], catalog)
        if problem is not None:
            raise problem
    with platform_yaml_lock(yaml_path.parent, timeout_s=lock_timeout_s):
        document, layout, config = _load_disk_config(yaml_path)
        entries = config.get("locked_rules")
        if entries is not None and not isinstance(entries, list):
            raise _persist_failed(
                yaml_path,
                "unparseable",
                "`authorization.config.locked_rules` must be a list; "
                f"got {type(entries).__name__}",
            )
        before = _compile_disk_locked(yaml_path, _plain(entries or []), catalog)
        if isinstance(op, InsertLockedRule):
            new_id = derive_locked_rule_id(op.rule.what, op.rule.pattern, "deny")
            if any(r.id == new_id for r in before):
                raise AuthzRuleInvalid(
                    position=-1,
                    reason=f"locked rule {new_id} already exists",
                    where="cli.permissions.mutate_platform_rules",
                )
            if entries is None:
                config["locked_rules"] = []
                entries = config["locked_rules"]
            if not entries:
                with contextlib.suppress(AttributeError):
                    entries.fa.set_block_style()
            position = len(entries)
            seq_insert_preserving_comments(entries, position, _rule_to_dict(op.rule))
            after = _compile_disk_locked(yaml_path, _plain(entries), catalog)
            rule = after[position]
        else:
            found = next((i for i, r in enumerate(before) if r.id == op.rule_id), None)
            if found is None or entries is None:
                raise AuthzRuleNotFound(
                    rule_id=op.rule_id,
                    available_ids=[r.id for r in before],
                    where="cli.permissions.mutate_platform_rules",
                )
            position, rule = found, before[found]
            seq_delete_preserving_comments(entries, found)
            if not entries:
                del config["locked_rules"]
            after = _compile_disk_locked(yaml_path, _plain(entries), catalog)
        _write_document(yaml_path, document, layout)
    return LockedRulesMutationResult(
        written=True,
        position=position,
        rule=rule,
        disk_locked_before=before,
        disk_locked_after=after,
    )


def _locked_rule_compile_problem(
    entries: list[Any], catalog: AssetCatalog | None
) -> PlatformError | None:
    """Compile `entries` as `locked_rules`; return the error, or `None` if valid."""
    try:
        RuleBasedAuthorizationProvider(
            config={"locked_rules": entries}, catalog=catalog
        )
    except PlatformError as exc:
        return exc
    return None


def _compile_disk_locked(
    yaml_path: Path, entries: list[Any], catalog: AssetCatalog | None
) -> list[Rule]:
    """Compile the disk `locked_rules`; refuse to write over an invalid list."""
    try:
        provider = RuleBasedAuthorizationProvider(
            config={"locked_rules": entries}, catalog=catalog
        )
    except Exception as exc:  # any compile failure — refuse to write
        raise _persist_failed(yaml_path, "invalid_rules", str(exc)) from exc
    return provider.locked_rules_snapshot()


def _rule_key(rule: Rule) -> tuple[str | None, str, str, str | None, str]:
    """Structural identity: every field that changes what a rule matches."""
    return (rule.who, rule.what, rule.pattern, rule.from_, rule.effect)


def _scope_key(rule: Rule) -> tuple[str, str, str | None]:
    """`/perm` identity: `(what, pattern, from)` — the effect is what changes."""
    return (rule.what, rule.pattern, rule.from_)


# TODO(bug-152): switch to `providers.authz.inspect_default_deny_tail`
# once #22 lands; this is the same shape test, kept private until then.
def _is_tail_shape(entry: Mapping[str, Any]) -> bool:
    """Return whether a raw disk entry is an unscoped catch-all deny.

    Canonical `{what: "*", pattern: "*" (or absent), effect: deny}`
    with no `who`, or legacy `{who: ".*", what: ".*", effect: deny}`;
    either disqualified by a non-null `from` / `from_`.
    """
    if entry.get("effect") != "deny":
        return False
    if entry.get("from") is not None or entry.get("from_") is not None:
        return False
    who = entry.get("who")
    if who is not None:
        return bool(who == ".*" and entry.get("what") == ".*")
    return bool(entry.get("what") == "*" and entry.get("pattern", "*") == "*")


def _disk_tail_index(entries: Sequence[Any]) -> int:
    """Index before the first tail-shaped disk entry, else the end."""
    for index, entry in enumerate(entries):
        if isinstance(entry, dict) and _is_tail_shape(entry):
            return index
    return len(entries)


def _first_new_shape_scope_match(rules: list[Rule], target: Rule) -> int | None:
    for index, rule in enumerate(rules):
        if rule.who is None and _scope_key(rule) == _scope_key(target):
            return index
    return None


def _plan_mutation(
    op: RulesOp, before: list[Rule], entries: list[Any]
) -> _MutationPlan:
    """Resolve `op` to disk indices (no mutation yet)."""
    if isinstance(op, InsertRule):
        return _plan_insert(op, before, entries)
    if isinstance(op, UpsertRule):
        drop = _first_new_shape_scope_match(before, op.rule)
        if drop is not None and (
            before[drop].id == DEFAULT_DENY_RULE_ID or _is_tail_shape(entries[drop])
        ):
            raise _protected(before[drop].id)
        remaining = [e for i, e in enumerate(entries) if i != drop]
        return _MutationPlan(
            insert_at=_disk_tail_index(remaining),
            insert_rule=op.rule,
            remove_at=drop,
            report_at=drop,
        )
    if op.rule_id is not None:
        index = next((i for i, r in enumerate(before) if r.id == op.rule_id), None)
        if index is None:
            raise AuthzRuleNotFound(
                rule_id=op.rule_id,
                available_ids=[r.id for r in before],
                where="cli.permissions.mutate_platform_rules",
            )
    elif op.match is not None:
        index = _first_new_shape_scope_match(before, op.match)
        if index is None:
            return _MutationPlan()
    else:  # unreachable: `_check_op` requires exactly one
        return _MutationPlan()
    if before[index].id == DEFAULT_DENY_RULE_ID or _is_tail_shape(entries[index]):
        raise _protected(before[index].id)
    return _MutationPlan(remove_at=index, report_at=index)


def _plan_insert(
    op: InsertRule, before: list[Rule], entries: list[Any]
) -> _MutationPlan:
    if op.position == "first":
        target = 0
    elif op.position in ("before", "after"):
        found = next((i for i, r in enumerate(before) if r.id == op.ref_id), None)
        if found is None:
            raise AuthzRuleInvalid(
                position=-1,
                reason=f"`{op.position}` rule id {op.ref_id!r} not found",
                where="cli.permissions.mutate_platform_rules",
            )
        target = found if op.position == "before" else found + 1
    else:
        anchor = op.anchor if op.position == "anchor_or_tail" else None
        anchored = (
            next(
                (i for i, r in enumerate(before) if _rule_key(r) == _rule_key(anchor)),
                None,
            )
            if anchor is not None
            else None
        )
        target = anchored if anchored is not None else _disk_tail_index(entries)
    # D7 — an identical rule at or before the slot already wins every
    # match the new one would: inserting it changes nothing.
    duplicate = next(
        (
            i
            for i, r in enumerate(before[: target + 1])
            if _rule_key(r) == _rule_key(op.rule)
        ),
        None,
    )
    if duplicate is not None:
        return _MutationPlan(report_at=duplicate)
    return _MutationPlan(insert_at=target, insert_rule=op.rule)


_LOCKED_SECTION_HEADER = (
    "locked (evaluated before workflow/agent/bundle layers; cannot be overridden):"
)


def _write_document(yaml_path: Path, document: Any, layout: RoundTripLayout) -> None:
    try:
        text = dump_round_trip_with_layout(document, layout)
        _atomic_write_text(yaml_path, text)
    except OSError as exc:
        raise _persist_failed(yaml_path, "write_failed", str(exc)) from exc


def _atomic_write_text(path: Path, text: str) -> None:
    """Replace `path` with `text`: temp file in the same dir, fsync, `os.replace`.

    Keeps the existing file's permission bits. On failure the temp
    file is removed and `path` is untouched.
    """
    fd, temp_name = tempfile.mkstemp(prefix=f"{path.name}.tmp.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            temp.chmod(stat.S_IMODE(path.stat().st_mode))
        os.replace(temp, path)  # noqa: PTH105 — atomic rename
    except OSError:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise


def persist_rule_request(
    yaml_path: Path,
    request: RulePersistRequest,
    *,
    catalog: AssetCatalog | None,
) -> RulesMutationResult:
    """Persist one chat "always" answer: insert `request.rule` on disk.

    Lands immediately before the disk copy of `request.anchor` (the
    platform `prompt` rule that asked), else before the default-deny
    tail, else at the end — computed on the rules **currently** in
    `yaml_path`. What the chat's `on_rule_added` hook runs.
    """
    return mutate_platform_rules(
        yaml_path,
        InsertRule(request.rule, anchor=request.anchor, position="anchor_or_tail"),
        catalog=catalog,
    )


def rule_identity(rule: Rule) -> tuple[str | None, str, str, str | None, str]:
    """Return a rule's structural identity `(who, what, pattern, from, effect)`.

    Two rules with equal identity match exactly the same calls. Ignores
    `id` (derived, and blind to `from:`) and source-location metadata.
    """
    return _rule_key(rule)


def permissions_add_command(rule: Rule) -> str:
    """Return the `kolsch permissions add …` command that persists `rule`."""
    parts = [
        "kolsch permissions add",
        f"--what {shlex.quote(rule.what)}",
        f"--pattern {shlex.quote(rule.pattern)}",
    ]
    if rule.from_ is not None:
        parts.append(f"--from {shlex.quote(rule.from_)}")
    parts.append(f"--effect {rule.effect}")
    return " ".join(parts)
