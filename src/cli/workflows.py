"""``kolsch workflows`` CLI — Phase 3.0 Step 6.

Five subcommands:

- ``list`` — print catalog entries with ``asset_type="workflow"``.
- ``show <ref>`` — print parsed-workflow structure.
- ``validate <ref>`` — run the Step 5 gate via
  :func:`workflows.validator.validate_workflow_or_raise`.
- ``cancel <run_id>`` — R3 #1 — atomically write a sidecar
  ``cancel.flag`` into the run directory.
- ``run <ref>`` — STUB at Phase 3.0; ships in Phase 3.1.

All commands accept ``--format text|json``. The JSON envelope follows
master spec §14.3; per-error objects follow §14.4 + R3 #8 + R3 #11
via :func:`cli._format.render_exception`. Text/json branching happens
exclusively inside :func:`cli._format.render` — no subcommand prints
payload directly.

R3 #1 — cancel protocol split between Phase 3.0 (CLI write) and
Phase 3.1 (executor cooperation + orphan sweep). See the command's
docstring for the locked contract.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, Any, TextIO

from assets import AssetManagement
from cli._format import render, render_exception
from workflows.errors import WorkflowError, WorkflowValidationError
from workflows.parser import load_workflow
from workflows.validator import validate_workflow_or_raise

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["add_workflows_subparser", "cmd_workflows"]


# ---------------------------------------------------------------------------
# Subparser registration.
# ---------------------------------------------------------------------------


def add_workflows_subparser(
    sub: argparse._SubParsersAction[Any],
) -> argparse.ArgumentParser:
    """Register the ``kolsch workflows`` subparser tree."""
    workflows: argparse.ArgumentParser = sub.add_parser(
        "workflows",
        help="Manage workflow assets (list, show, validate, cancel, run).",
    )
    workflows.add_argument(
        "--format",
        dest="format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    workflows_sub = workflows.add_subparsers(
        dest="workflows_command", metavar="<op>"
    )

    workflows_sub.add_parser("list", help="List catalogued workflows.")

    show = workflows_sub.add_parser(
        "show", help="Print a workflow's parsed structure."
    )
    show.add_argument("ref", help="Catalog ref (e.g. acme/workflows/triage).")

    validate = workflows_sub.add_parser(
        "validate", help="Run the Step 5 run-start validation gate."
    )
    validate.add_argument("ref", help="Catalog ref.")

    cancel = workflows_sub.add_parser(
        "cancel",
        help=(
            "Request cancellation by writing a sidecar cancel.flag (R3 #1). "
            "Phase 3.0 ships flag-write only; executor cooperation lands "
            "in Phase 3.1."
        ),
    )
    cancel.add_argument("run_id", help="Workflow run id.")

    run = workflows_sub.add_parser(
        "run",
        help="STUB — workflow execution ships in Phase 3.1.",
    )
    run.add_argument("ref", help="Catalog ref.")

    return workflows


# ---------------------------------------------------------------------------
# Top-level dispatcher.
# ---------------------------------------------------------------------------


async def cmd_workflows(
    args: argparse.Namespace,
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    """Route to one of the five leaf subcommands."""
    op = args.workflows_command
    fmt: str = getattr(args, "format", "text") or "text"

    if op is None:
        err.write(
            "usage: kolsch workflows <op>\n"
            "ops: list, show, validate, cancel, run\n"
        )
        return 1

    am = AssetManagement(root=args.root)
    await am.initialize()
    catalog = am.catalog()

    if op == "list":
        return _cmd_list(catalog, fmt=fmt, out=out)
    if op == "show":
        return _cmd_show(catalog, args.ref, fmt=fmt, out=out, err=err)
    if op == "validate":
        return _cmd_validate(catalog, args.ref, fmt=fmt, out=out, err=err)
    if op == "cancel":
        return _cmd_cancel(args.root, args.run_id, fmt=fmt, out=out, err=err)
    if op == "run":
        return _cmd_run_stub(args.ref, fmt=fmt, out=out, err=err)

    err.write(f"unknown workflows op {op!r}\n")
    return 1


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _cmd_list(catalog: Any, *, fmt: str, out: TextIO) -> int:  # noqa: ANN401
    rows = [
        {
            "ref": entry.ref,
            "kind": entry.metadata.get("kind", "workflow"),
            "description": entry.metadata.get("description", "")[:50],
        }
        for entry in catalog.assets.values()
        if entry.asset_type == "workflow"
    ]
    rows.sort(key=lambda r: r["ref"])
    if fmt == "json":
        out.write(
            render(
                command="workflows list",
                fmt=fmt,
                data={"workflows": rows},
            )
        )
        out.write("\n")
        return 0
    if not rows:
        out.write("(no workflows)\n")
        return 0
    lines = ["ref\tkind\tdescription"]
    lines.extend(f"{r['ref']}\t{r['kind']}\t{r['description']}" for r in rows)
    out.write(
        render(
            command="workflows list",
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
    entry = catalog.lookup(ref, expected_type="workflow")
    if entry is None:
        return _emit_unknown_ref(
            command="workflows show", ref=ref, fmt=fmt, out=out, err=err
        )
    try:
        from pathlib import Path  # noqa: PLC0415

        workflow = load_workflow(Path(entry.path))
    except WorkflowError as exc:
        return _emit_error_envelope(
            command="workflows show",
            errors=[exc],
            fmt=fmt,
            out=out,
            err=err,
        )

    payload = {
        "ref": entry.ref,
        "kind": "workflow" if workflow.graph is not None else "node_library",
        "description": workflow.description,
        "extends": list(workflow.extends),
        "nodes": {
            name: {"type": getattr(node, "type", "<copy_of>")}
            for name, node in workflow.nodes.items()
        },
        "edges": [
            {"from": e.from_, "to": e.to}
            for e in (workflow.graph.edges if workflow.graph is not None else [])
        ],
    }
    if fmt == "json":
        out.write(
            render(
                command="workflows show",
                fmt=fmt,
                data={"workflow": payload},
            )
        )
        out.write("\n")
        return 0
    lines = [
        f"ref: {payload['ref']}",
        f"kind: {payload['kind']}",
        f"description: {payload['description'] or '-'}",
        f"extends: {', '.join(payload['extends']) or '-'}",
        f"nodes ({len(payload['nodes'])}):",
    ]
    for name, node in payload["nodes"].items():
        lines.append(f"  {name}\t{node['type']}")
    lines.append(f"edges ({len(payload['edges'])}):")
    for e in payload["edges"]:
        lines.append(f"  {e['from']} -> {e['to']}")
    out.write(
        render(
            command="workflows show",
            fmt=fmt,
            text_body="\n".join(lines) + "\n",
        )
    )
    return 0


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def _cmd_validate(
    catalog: Any,  # noqa: ANN401
    ref: str,
    *,
    fmt: str,
    out: TextIO,
    err: TextIO,
) -> int:
    entry = catalog.lookup(ref, expected_type="workflow")
    if entry is None:
        return _emit_unknown_ref(
            command="workflows validate", ref=ref, fmt=fmt, out=out, err=err
        )

    try:
        from pathlib import Path  # noqa: PLC0415

        workflow = load_workflow(Path(entry.path))
    except WorkflowError as exc:
        return _emit_error_envelope(
            command="workflows validate",
            errors=[exc],
            fmt=fmt,
            out=out,
            err=err,
        )

    try:
        validate_workflow_or_raise(workflow, catalog, file=entry.path)
    except ExceptionGroup as group:
        # Multiple errors → split into one per sub-exception.
        sub_errors = [
            exc
            for exc in group.exceptions
            if isinstance(exc, WorkflowError)
        ]
        return _emit_error_envelope(
            command="workflows validate",
            errors=sub_errors,
            fmt=fmt,
            out=out,
            err=err,
        )
    except WorkflowValidationError as exc:
        return _emit_error_envelope(
            command="workflows validate",
            errors=[exc],
            fmt=fmt,
            out=out,
            err=err,
        )

    # Success.
    if fmt == "json":
        out.write(
            render(
                command="workflows validate",
                fmt=fmt,
                data={"workflow_ref": entry.ref, "valid": True},
            )
        )
        out.write("\n")
        return 0
    out.write(
        render(
            command="workflows validate",
            fmt=fmt,
            text_body=f"valid: {entry.ref}\n",
        )
    )
    return 0


# ---------------------------------------------------------------------------
# cancel (R3 #1)
# ---------------------------------------------------------------------------


def _cmd_cancel(
    root: Path,
    run_id: str,
    *,
    fmt: str,
    out: TextIO,
    err: TextIO,
) -> int:
    """R3 #1 — atomically write ``<workspace>/runs/<run_id>/cancel.flag``.

    Phase 3.0 ships ONLY the CLI + flag-write contract. The executor
    polls this file between node boundaries in Phase 3.1; engine
    startup runs an orphan sweep that reconciles ``status=running``
    runs without a live PID. The 3.0 CLI does NOT signal any process
    and does NOT check executor liveness — write the flag and exit.
    """
    runs_dir = root / "runs"
    run_dir = runs_dir / run_id
    if not run_dir.is_dir():
        msg = (
            f"run {run_id!r} not found under {runs_dir}"
        )
        if fmt == "json":
            out.write(
                render(
                    command="workflows cancel",
                    fmt=fmt,
                    status="error",
                    errors=[{"class": "RunNotFound", "summary": msg}],
                )
            )
            out.write("\n")
        else:
            err.write(msg + "\n")
        return 1
    flag = run_dir / "cancel.flag"
    tmp = run_dir / "cancel.flag.tmp"
    tmp.write_text("", encoding="utf-8")
    tmp.replace(flag)
    message = (
        f"cancel requested for {run_id}; executor will halt at the "
        "next node boundary"
    )
    if fmt == "json":
        out.write(
            render(
                command="workflows cancel",
                fmt=fmt,
                data={
                    "run_id": run_id,
                    "cancel_flag_path": str(flag),
                    "message": message,
                },
            )
        )
        out.write("\n")
    else:
        out.write(message + "\n")
    return 0


# ---------------------------------------------------------------------------
# run — STUB
# ---------------------------------------------------------------------------


_RUN_STUB_MESSAGE = (
    "Workflow execution is not implemented in Phase 3.0. "
    "Ships in Phase 3.1."
)


def _cmd_run_stub(
    ref: str,
    *,
    fmt: str,
    out: TextIO,
    err: TextIO,
) -> int:
    _ = ref
    if fmt == "json":
        out.write(
            render(
                command="workflows run",
                fmt=fmt,
                status="error",
                errors=[
                    {
                        "class": "NotImplemented",
                        "summary": _RUN_STUB_MESSAGE,
                    }
                ],
            )
        )
        out.write("\n")
    else:
        err.write(_RUN_STUB_MESSAGE + "\n")
    return 1


# ---------------------------------------------------------------------------
# Shared error-envelope helpers.
# ---------------------------------------------------------------------------


def _emit_unknown_ref(
    *,
    command: str,
    ref: str,
    fmt: str,
    out: TextIO,
    err: TextIO,
) -> int:
    msg = f"unknown workflow ref {ref!r}"
    if fmt == "json":
        out.write(
            render(
                command=command,
                fmt=fmt,
                status="error",
                errors=[
                    {
                        "class": "WorkflowNotFound",
                        "summary": msg,
                    }
                ],
            )
        )
        out.write("\n")
    else:
        err.write(msg + "\n")
    return 1


def _emit_error_envelope(
    *,
    command: str,
    errors: list[WorkflowError],
    fmt: str,
    out: TextIO,
    err: TextIO,
) -> int:
    error_dicts = [render_exception(exc) for exc in errors]
    if fmt == "json":
        out.write(
            render(
                command=command,
                fmt=fmt,
                status="error",
                errors=error_dicts,
            )
        )
        out.write("\n")
    else:
        for exc in errors:
            err.write(str(exc) + "\n")
    return 1
