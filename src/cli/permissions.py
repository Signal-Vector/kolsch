"""`kolsch permissions {list, add, remove, show}` CLI surface.

The out-of-chat editor for the workspace's authorization rule list.
Routes through the in-memory
[`RuleBasedAuthorizationProvider`][providers.RuleBasedAuthorizationProvider]
runtime API (`list_rules` / `add_rule` / `remove_rule` / `get_rule`)
so the same rule-id scheme, default-deny protection, and atomicity
discipline as the in-chat modal apply here too.

Atomic writes: `platform.yaml` is rewritten via temp + fsync +
`os.replace`, the same pattern `llm_commands.py` uses for
`platform.yaml` writes.

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
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TextIO

from assets.platform_config import authorization_rule_source_map, platform_yaml_lock
from errors import (
    AuthzProtectedRule,
    AuthzRuleInvalid,
    AuthzRuleNotFound,
    AuthzRulesPersistFailed,
    WorkspaceNotInitialized,
)
from kolsch._yamlio import (
    YAMLError,
    dump_preserving_trailing_comment_block,
    dump_round_trip_with_layout,
    load,
    load_round_trip_with_layout,
    seq_delete_preserving_comments,
    seq_insert_preserving_comments,
)
from platform_runtime.workspace import workspace_is_initialized
from providers import (
    DEFAULT_DENY_RULE_ID,
    Rule,
    RuleBasedAuthorizationProvider,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from assets.catalog import AssetCatalog
    from errors.authz import RulesPersistFailureReason
    from kolsch._yamlio import RoundTripLayout
    from providers.authz import RuleSourceMap

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
    )

    op = args.permissions_command
    if op == "list":
        return await _cmd_list(provider, out=out)
    if op == "show":
        return await _cmd_show(provider, args.rule_id, out=out, err=err)
    if op == "add":
        return await _cmd_add(provider, args, yaml_path=yaml_path, out=out, err=err)
    if op == "remove":
        return await _cmd_remove(
            provider, args.rule_id, yaml_path=yaml_path, out=out, err=err
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
    if rule.id == DEFAULT_DENY_RULE_ID:
        out.write("        (protected — removal refused)\n")
    return 0


async def _cmd_add(
    provider: RuleBasedAuthorizationProvider,
    args: argparse.Namespace,
    *,
    yaml_path: Path,
    out: TextIO,
    err: TextIO,
) -> int:
    new_rule = Rule(
        what=args.what,
        pattern=args.pattern,
        from_=args.from_,
        effect=args.effect,
    )
    kwargs: dict[str, Any] = {}
    if args.before:
        kwargs["before"] = args.before
    elif args.after:
        kwargs["after"] = args.after
    elif args.first:
        kwargs["first"] = True
    try:
        added = await provider.add_rule(new_rule, **kwargs)
    except AuthzRuleInvalid as exc:
        err.write(f"error: {exc}\n")
        return 1
    _write_back(yaml_path, await provider.list_rules())
    from_str = f" from={added.from_!r}" if added.from_ is not None else ""
    out.write(
        f"added rule {added.id}: what={added.what!r} "
        f"pattern={added.pattern!r}{from_str} effect={added.effect}\n"
    )
    return 0


async def _cmd_remove(
    provider: RuleBasedAuthorizationProvider,
    rule_id: str,
    *,
    yaml_path: Path,
    out: TextIO,
    err: TextIO,
) -> int:
    try:
        await provider.remove_rule(rule_id)
    except AuthzProtectedRule as exc:
        err.write(f"error: {exc}\n")
        return 1
    except AuthzRuleNotFound as exc:
        err.write(f"error: {exc}\n")
        return 1
    _write_back(yaml_path, await provider.list_rules())
    out.write(f"removed rule {rule_id}\n")
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


def _write_back(yaml_path: Path, rules: list[Rule]) -> None:
    """Atomically rewrite platform.yaml's `authorization.config.rules`.

    Preserves every other section of the file verbatim — only the
    rules list is replaced. Temp + fsync + `os.replace` for
    crash-safety.

    qol-133: uses `dump_preserving_trailing_comment_block` (not raw
    `dump()`) so a trailing, fully-commented scaffold block already
    on disk — e.g. the fresh-install `hooks:` example — survives a
    standalone `kolsch permissions` edit. No-op when there's no such
    block.
    """
    previous_text = yaml_path.read_text(encoding="utf-8") if yaml_path.is_file() else ""
    payload: dict[str, Any] = {}
    if previous_text:
        existing = load(previous_text)
        if isinstance(existing, dict):
            payload = existing

    authz = payload.setdefault("authorization", {})
    if not isinstance(authz, dict):
        authz = {}
        payload["authorization"] = authz
    authz.setdefault("variant", "rule_based")
    cfg = authz.setdefault("config", {})
    if not isinstance(cfg, dict):
        cfg = {}
        authz["config"] = cfg
    cfg["rules"] = [_rule_to_dict(r) for r in rules]

    serialized = dump_preserving_trailing_comment_block(previous_text, payload)
    temp = yaml_path.with_name(f"{yaml_path.name}.tmp.{os.getpid()}")
    try:
        encoded = serialized.encode("utf-8")
        with temp.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, yaml_path)  # noqa: PTH105 — atomic rename
    except OSError:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise


def _rule_to_dict(rule: Rule) -> dict[str, str]:
    """Serialize a Rule for round-trip.

    New-shape rules (`who is None`) serialize without `id` / `who` and
    carry `pattern` (plus `from` when scoped); legacy rules still
    serialize with the old `{id, who, what, effect}` shape so an
    unmigrated workspace that gets one rule added/removed via the
    CLI doesn't accidentally flip back to the new shape mid-file.
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


def mutate_platform_rules(
    yaml_path: Path,
    op: RulesOp,
    *,
    catalog: AssetCatalog | None,
    lock_timeout_s: float = 5.0,
) -> RulesMutationResult:
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
        AuthzRulesPersistFailed: `missing` / `unparseable` /
            `invalid_rules` / `lock_timeout` / `write_failed` — nothing
            was written.
        AuthzProtectedRule: The op would remove the default-deny tail.
        AuthzRuleNotFound: `RemoveRule(rule_id=)` names no disk rule.
        AuthzRuleInvalid: A malformed op, or `before` / `after` names
            no disk rule.
        PlatformError: The new rule itself fails to compile (e.g.
            `authz.pattern_invalid`); raised as the provider raises it.
    """
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
        RuleBasedAuthorizationProvider(
            config={"rules": [_rule_to_dict(op_rule)]}, catalog=catalog
        )
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
    if not yaml_path.is_file():
        raise _persist_failed(
            yaml_path, "missing", "platform.yaml does not exist; not creating it"
        )
    try:
        text = yaml_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _persist_failed(yaml_path, "unparseable", f"cannot read: {exc}") from exc
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
