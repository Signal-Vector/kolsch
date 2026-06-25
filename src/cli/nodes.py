"""``kolsch nodes`` CLI — Phase 3.0 Step 6 (Round 7 L7-12).

Two subcommands, symmetric in shape with ``kolsch workflows``:

- ``list [--bundle <name>]`` — iterate catalog entries with
  ``asset_type="node"``; optional ``--bundle <name>`` filter.
- ``show <ref>`` — introspect one node (catalog lookup; full
  introspection lands when the Phase 3.1 executor wires up
  ``@node`` runtime registration).

Both commands accept ``--format text|json``; output routes through
the shared :func:`cli._format.render` helper.

``kolsch nodes run`` is NOT shipped at Phase 3.0; deferred to Phase 3.1+.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, TextIO

from assets import AssetManagement
from cli._format import render

__all__ = ["add_nodes_subparser", "cmd_nodes"]


# ---------------------------------------------------------------------------
# Subparser registration.
# ---------------------------------------------------------------------------


def add_nodes_subparser(
    sub: argparse._SubParsersAction[Any],
) -> argparse.ArgumentParser:
    """Register the ``kolsch nodes`` subparser tree."""
    nodes: argparse.ArgumentParser = sub.add_parser(
        "nodes",
        help="Discover node assets (list, show).",
    )
    nodes.add_argument(
        "--format",
        dest="format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    nodes_sub = nodes.add_subparsers(dest="nodes_command", metavar="<op>")

    list_cmd = nodes_sub.add_parser("list", help="List catalogued nodes.")
    list_cmd.add_argument(
        "--bundle",
        dest="bundle",
        default=None,
        help="Filter results by bundle name (e.g. --bundle kolsch).",
    )

    show = nodes_sub.add_parser(
        "show", help="Print a node's catalog entry + introspection."
    )
    show.add_argument("ref", help="Catalog ref (e.g. acme/nodes/echo).")

    return nodes


# ---------------------------------------------------------------------------
# Top-level dispatcher.
# ---------------------------------------------------------------------------


async def cmd_nodes(
    args: argparse.Namespace,
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    op = args.nodes_command
    fmt: str = getattr(args, "format", "text") or "text"

    if op is None:
        err.write(
            "usage: kolsch nodes <op>\n"
            "ops: list, show\n"
        )
        return 1

    am = AssetManagement(root=args.root)
    await am.initialize()
    catalog = am.catalog()

    if op == "list":
        return _cmd_list(
            catalog,
            bundle=getattr(args, "bundle", None),
            fmt=fmt,
            out=out,
        )
    if op == "show":
        return _cmd_show(catalog, args.ref, fmt=fmt, out=out, err=err)

    err.write(f"unknown nodes op {op!r}\n")
    return 1


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _cmd_list(
    catalog: Any,  # noqa: ANN401
    *,
    bundle: str | None,
    fmt: str,
    out: TextIO,
) -> int:
    rows: list[dict[str, Any]] = []
    for entry in catalog.assets.values():
        if entry.asset_type != "node":
            continue
        if bundle is not None and entry.bundle != bundle:
            continue
        label = entry.ref.rsplit("/", 1)[-1]
        rows.append(
            {
                "ref": entry.ref,
                "kind": "node",
                "label": label,
                "secrets_needed": [],
            }
        )
    rows.sort(key=lambda r: r["ref"])
    if fmt == "json":
        out.write(
            render(
                command="nodes list",
                fmt=fmt,
                data={"nodes": rows},
            )
        )
        out.write("\n")
        return 0
    if not rows:
        out.write("(no nodes)\n")
        return 0
    lines = ["ref\tkind\tlabel\tsecrets_needed"]
    for r in rows:
        secrets = ", ".join(r["secrets_needed"]) or "-"
        lines.append(f"{r['ref']}\t{r['kind']}\t{r['label']}\t{secrets}")
    out.write(
        render(
            command="nodes list",
            fmt=fmt,
            text_body="\n".join(lines) + "\n",
        )
    )
    return 0


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def _cmd_show(
    catalog: Any,  # noqa: ANN401
    ref: str,
    *,
    fmt: str,
    out: TextIO,
    err: TextIO,
) -> int:
    entry = catalog.lookup(ref, expected_type="node")
    if entry is None:
        msg = f"unknown node ref {ref!r}"
        if fmt == "json":
            out.write(
                render(
                    command="nodes show",
                    fmt=fmt,
                    status="error",
                    errors=[
                        {
                            "class": "NodeNotFound",
                            "summary": msg,
                        }
                    ],
                )
            )
            out.write("\n")
        else:
            err.write(msg + "\n")
        return 1

    label = entry.ref.rsplit("/", 1)[-1]
    payload = {
        "ref": entry.ref,
        "bundle": entry.bundle,
        "path": entry.path,
        "label": label,
        "signature": None,
        "inputs_schema": None,
        "outputs_schema": None,
        "secrets_needed": [],
        "description": entry.metadata.get("description"),
    }
    if fmt == "json":
        out.write(
            render(
                command="nodes show",
                fmt=fmt,
                data={"node": payload},
            )
        )
        out.write("\n")
        return 0
    lines = [
        f"ref: {payload['ref']}",
        f"bundle: {payload['bundle']}",
        f"label: {payload['label']}",
        f"path: {payload['path'] or '<virtual>'}",
        f"description: {payload['description'] or '-'}",
        f"secrets_needed: {', '.join(payload['secrets_needed']) or '-'}",
    ]
    out.write(
        render(
            command="nodes show",
            fmt=fmt,
            text_body="\n".join(lines) + "\n",
        )
    )
    return 0
