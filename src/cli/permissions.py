"""``kolsch permissions {list, add, remove, show}`` — Phase 2.0, TOOL-R6.

The out-of-chat editor for the workspace's authorization rule list.
Routes through the in-memory :class:`RuleBasedAuthorizationProvider`
runtime API (``list_rules`` / ``add_rule`` / ``remove_rule`` /
``get_rule``) so the same rule-id scheme, default-deny protection,
and atomicity discipline that the in-chat modal uses (Phase 2.1)
also apply here.

Atomic writes: ``platform.yaml`` is rewritten via temp + fsync +
``os.replace`` — same pattern as ``llm_commands.py`` uses for
``platform.yaml`` writes (Phase 1.1 atomicity discipline).
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from pathlib import Path
from typing import Any, TextIO

import yaml

from errors import (
    AuthzProtectedRule,
    AuthzRuleInvalid,
    AuthzRuleNotFound,
    WorkspaceNotInitialized,
)
from platform_runtime.workspace import workspace_is_initialized
from providers import (
    DEFAULT_DENY_RULE_ID,
    Rule,
    RuleBasedAuthorizationProvider,
)

# ---------------------------------------------------------------------------
# Argparse wiring — called from cli.main.build_parser
# ---------------------------------------------------------------------------


def add_permissions_subparser(
    sub: argparse._SubParsersAction[Any],
) -> argparse.ArgumentParser:
    """Register the ``kolsch permissions`` subparser tree.

    Four leaf subcommands mirror the runtime API on
    :class:`RuleBasedAuthorizationProvider`. Returns the parent
    ``permissions`` subparser so the dispatcher can stash a reference
    for the help-on-missing-op path
    (bug-sessions-namespace-attr-error.md).
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
    add.add_argument("--who", required=True, help="Principal regex.")
    add.add_argument("--what", required=True, help="Action regex.")
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
        help="Remove a rule by id (refuses rl_default_deny per TOOL-R10).",
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
    """Dispatch the four leaf ops. Returns the process exit code."""
    if not workspace_is_initialized(args.root):
        raise WorkspaceNotInitialized(path=str(args.root), env_var_set=False)

    yaml_path = args.root / "platform.yaml"
    config = _read_authz_config(yaml_path)
    provider = RuleBasedAuthorizationProvider(config=config)

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
    rules = await provider.list_rules()
    if not rules:
        out.write("(no rules)\n")
        return 0
    # Column widths for readability.
    id_w = max(len(r.id) for r in rules)
    effect_w = max(len(r.effect) for r in rules)
    for rule in rules:
        protected = "  (protected)" if rule.id == DEFAULT_DENY_RULE_ID else ""
        out.write(
            f"{rule.id:<{id_w}}  {rule.effect:<{effect_w}}  "
            f"who={rule.who!r}  what={rule.what!r}{protected}\n"
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
    out.write(f"who:    {rule.who!r}\n")
    out.write(f"what:   {rule.what!r}\n")
    out.write(f"effect: {rule.effect}\n")
    if rule.id == DEFAULT_DENY_RULE_ID:
        out.write("        (protected — removal refused per TOOL-R10)\n")
    return 0


async def _cmd_add(
    provider: RuleBasedAuthorizationProvider,
    args: argparse.Namespace,
    *,
    yaml_path: Path,
    out: TextIO,
    err: TextIO,
) -> int:
    new_rule = Rule(who=args.who, what=args.what, effect=args.effect)
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
    out.write(
        f"added rule {added.id}: who={added.who!r} "
        f"what={added.what!r} effect={added.effect}\n"
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
    payload = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
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
    """Atomically rewrite platform.yaml's ``authorization.config.rules``.

    Preserves every other section of the file verbatim — only the
    rules list is replaced. Temp + fsync + ``os.replace`` for
    crash-safety per Phase 1.1's discipline.
    """
    payload: dict[str, Any] = {}
    if yaml_path.is_file():
        existing = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
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

    serialized = yaml.safe_dump(payload, sort_keys=False)
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
    """Serialize a Rule for round-trip — preserve the id field so the
    next load gets stable references even if the user later edits the
    YAML by hand and the id-derivation hashes diverge."""
    return {
        "id": rule.id,
        "who": rule.who,
        "what": rule.what,
        "effect": rule.effect,
    }
