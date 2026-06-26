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


def _build_format_parent() -> argparse.ArgumentParser:
    """Tiny parent parser that defines ``--format`` once.

    Step 6 AC 6.7 — ``--format`` must be accepted at BOTH the group
    level (``workflows --format json list``) AND the subcommand level
    (``workflows list --format json``). Argparse subparsers do NOT
    inherit parent flags by default; the canonical fix is to define
    the flag on a ``parents=[…]`` parser shared by every parser in
    the tree. ``add_help=False`` keeps the helper from injecting a
    second ``-h``.
    """
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--format",
        dest="format",
        choices=("text", "json"),
        # ``SUPPRESS`` prevents the parent's argparse pass from
        # writing ``format=None`` over a value that the group-level
        # pass already set. The dispatcher applies the ``"text"``
        # default via ``getattr(args, "format", "text") or "text"``.
        default=argparse.SUPPRESS,
        help="Output format (default: text).",
    )
    return parent


def add_workflows_subparser(
    sub: argparse._SubParsersAction[Any],
) -> argparse.ArgumentParser:
    """Register the ``kolsch workflows`` subparser tree."""
    fmt_parent = _build_format_parent()
    workflows: argparse.ArgumentParser = sub.add_parser(
        "workflows",
        help="Manage workflow assets (list, show, validate, cancel, run).",
        parents=[fmt_parent],
    )
    workflows_sub = workflows.add_subparsers(
        dest="workflows_command", metavar="<op>"
    )

    workflows_sub.add_parser(
        "list", help="List catalogued workflows.", parents=[fmt_parent]
    )

    show = workflows_sub.add_parser(
        "show",
        help="Print a workflow's parsed structure.",
        parents=[fmt_parent],
    )
    show.add_argument("ref", help="Catalog ref (e.g. acme/workflows/triage).")

    validate = workflows_sub.add_parser(
        "validate",
        help="Run the Step 5 run-start validation gate.",
        parents=[fmt_parent],
    )
    validate.add_argument("ref", help="Catalog ref.")

    cancel = workflows_sub.add_parser(
        "cancel",
        help=(
            "Request cancellation by writing a sidecar cancel.flag (R3 #1). "
            "Phase 3.0 ships flag-write only; executor cooperation lands "
            "in Phase 3.1."
        ),
        parents=[fmt_parent],
    )
    cancel.add_argument("run_id", help="Workflow run id.")

    run = workflows_sub.add_parser(
        "run",
        help="Execute a workflow end-to-end.",
        parents=[fmt_parent],
    )
    run.add_argument("ref", help="Catalog ref (e.g. acme/workflows/triage).")
    run.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Initial workflow input; repeatable.",
    )
    run.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help=(
            "Print the resolved topological plan + per-node binds "
            "preview and exit 0 without firing the executor."
        ),
    )
    run.add_argument(
        "--follow",
        action="store_true",
        help=(
            "Tail ``events.jsonl`` while the run executes; one JSON "
            "line per event until terminal."
        ),
    )

    return workflows


# ---------------------------------------------------------------------------
# Top-level dispatcher.
# ---------------------------------------------------------------------------


# PLR0911 acknowledged: the dispatcher has one return per leaf
# subcommand (5) plus the no-op and unknown-op error paths. Each
# return tunnels the leaf's own exit code; collapsing into a mapping
# would lose the type-checker visibility of each leaf's signature
# differences (args.ref vs args.run_id vs no positional).
async def cmd_workflows(  # noqa: PLR0911
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
        return await _cmd_run(
            root=args.root,
            ref=args.ref,
            input_pairs=list(getattr(args, "input", None) or []),
            dry_run=bool(getattr(args, "dry_run", False)),
            follow=bool(getattr(args, "follow", False)),
            fmt=fmt,
            out=out,
            err=err,
        )

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
# run — Phase 3.1 Step 5 live executor wire-up
# ---------------------------------------------------------------------------


# PLR0913 acknowledged: the live ``run`` command surface is wide on
# purpose — ref, --input, --dry-run, --follow, --format, plus the
# captured out/err streams. Collapsing into an args object would only
# shift the parameter count behind another constructor; the dispatcher
# already passes them by keyword.
async def _cmd_run(  # noqa: PLR0913
    *,
    root: Path,
    ref: str,
    input_pairs: list[str],
    dry_run: bool,
    follow: bool,
    fmt: str,
    out: TextIO,
    err: TextIO,
) -> int:
    """Live ``kolsch workflows run <ref>`` handler.

    Three modes:

    - ``--dry-run`` prints the resolved topological plan + per-node
      raw ``binds`` preview and exits 0 without touching the executor
      (no JSONL written, no module imports).
    - ``--follow`` runs the executor concurrently with a JSONL tail
      that prints one line per event until the terminal event lands.
    - Default blocks until terminal; prints the final outputs JSON on
      success or the error envelope on failure.
    """
    from pathlib import Path as _Path  # noqa: PLC0415

    from platform_runtime import bootstrap_runtime  # noqa: PLC0415

    runtime = await bootstrap_runtime(root=root)
    catalog = runtime.asset_management.catalog()

    entry = catalog.lookup(ref, expected_type="workflow")
    if entry is None:
        return _emit_unknown_ref(
            command="workflows run", ref=ref, fmt=fmt, out=out, err=err
        )
    try:
        workflow = load_workflow(_Path(entry.path))
    except WorkflowError as exc:
        return _emit_error_envelope(
            command="workflows run",
            errors=[exc],
            fmt=fmt,
            out=out,
            err=err,
        )

    inputs = _parse_input_pairs(input_pairs)

    if dry_run:
        # AC 5.5 — print the plan WITHOUT touching the executor.
        # ``sys.modules`` is never mutated; no JSONL written; no
        # provider call.
        topo = _compute_dry_run_topology(workflow)
        return _emit_dry_run_plan(
            workflow=workflow,
            ref=entry.ref,
            topo=topo,
            inputs=inputs,
            fmt=fmt,
            out=out,
        )

    # Live executor wiring.
    from tools.builtins import make_bash  # noqa: PLC0415
    from tools.decorator import get_tool_definition  # noqa: PLC0415
    from workflows.builtins import register_builtins  # noqa: PLC0415
    from workflows.executor import (  # noqa: PLC0415
        IterativeExecutor,
        RunnerRegistry,
    )
    from workflows.run_store import WorkflowRunStore  # noqa: PLC0415

    store = WorkflowRunStore(root)
    run = store.create(workflow_ref=entry.ref, inputs=inputs)
    bash_fn = make_bash(policy=runtime.path_policy, root=root)
    bash_defn = get_tool_definition(bash_fn)
    registry = RunnerRegistry()
    register_builtins(
        registry,
        bash_defn=bash_defn,
        authz=runtime.authz,
        secrets=runtime.secrets,
        sandbox=runtime.sandbox,
        catalog=catalog,
    )
    executor = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=catalog,
    )

    if follow:
        return await _run_with_follow(
            executor=executor,
            store=store,
            run_id=run.run_id,
            fmt=fmt,
            out=out,
            err=err,
        )
    return await _run_blocking(
        executor=executor,
        store=store,
        run_id=run.run_id,
        fmt=fmt,
        out=out,
        err=err,
    )


def _parse_input_pairs(pairs: list[str]) -> dict[str, str]:
    """Parse ``--input KEY=VALUE`` repeats into a dict.

    Validation matches the ``kolsch run`` CLI surface: missing ``=``
    is a hard fail (``SystemExit``) so a malformed flag never
    silently drops state into the executor.
    """
    out: dict[str, str] = {}
    for item in pairs:
        if "=" not in item:
            msg = f"--input must be KEY=VALUE; got {item!r}"
            raise SystemExit(msg)
        key, value = item.split("=", 1)
        out[key] = value
    return out


def _compute_dry_run_topology(workflow: Any) -> list[str]:  # noqa: ANN401
    """Kahn's algorithm replicated here for the dry-run path.

    Duplicates :meth:`workflows.executor.IterativeExecutor._compute_topology`
    so the dry-run never imports the executor module (which would in
    turn load :mod:`workflows.module_isolation` + bundle plumbing —
    breaking AC 5.5).
    """
    from collections import deque  # noqa: PLC0415

    node_names = list(workflow.nodes)
    edges = (
        list(workflow.graph.edges) if workflow.graph is not None else []
    )
    incoming: dict[str, list[str]] = {n: [] for n in node_names}
    outgoing: dict[str, list[str]] = {n: [] for n in node_names}
    for edge in edges:
        if edge.from_ in incoming and edge.to in incoming:
            incoming[edge.to].append(edge.from_)
            outgoing[edge.from_].append(edge.to)
    in_degree = {n: len(incoming[n]) for n in node_names}
    queue: deque[str] = deque(n for n in node_names if in_degree[n] == 0)
    topo: list[str] = []
    while queue:
        current = queue.popleft()
        topo.append(current)
        for nxt in outgoing[current]:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)
    return topo


def _emit_dry_run_plan(  # noqa: PLR0913 — six explicit kw args; collapsing them into an args object would only shift the count
    *,
    workflow: Any,  # noqa: ANN401
    ref: str,
    topo: list[str],
    inputs: dict[str, str],
    fmt: str,
    out: TextIO,
) -> int:
    """Render the dry-run plan as text or JSON."""
    plan = [
        {
            "node": name,
            "type": getattr(
                workflow.nodes[name], "type", "<copy_of>"
            ),
            "binds": _node_binds_preview(workflow.nodes[name]),
            "when": getattr(workflow.nodes[name], "when", None),
        }
        for name in topo
    ]
    if fmt == "json":
        out.write(
            render(
                command="workflows run",
                fmt=fmt,
                data={
                    "workflow_ref": ref,
                    "dry_run": True,
                    "inputs": inputs,
                    "plan": plan,
                },
            )
        )
        out.write("\n")
        return 0
    lines = [
        f"dry-run plan: {ref}",
        f"inputs: {inputs or '-'}",
        f"topo ({len(plan)} nodes):",
    ]
    for step in plan:
        lines.append(f"  {step['node']}\t{step['type']}")
        if step["binds"]:
            lines.append(f"    binds: {step['binds']}")
        if step["when"] is not None:
            lines.append(f"    when:  {step['when']!r}")
    out.write(
        render(
            command="workflows run",
            fmt=fmt,
            text_body="\n".join(lines) + "\n",
        )
    )
    return 0


def _node_binds_preview(node_def: Any) -> dict[str, Any]:  # noqa: ANN401
    """Return the node's ``binds`` mapping in its raw, unrendered form.

    Jinja-marked values (``"{{ ... }}"``) are reported verbatim so the
    operator can see exactly what would render at runtime.
    """
    binds = getattr(node_def, "binds", None) or {}
    if isinstance(binds, dict):
        return dict(binds)
    return {}


async def _run_blocking(  # noqa: PLR0913 — six explicit kw args; shared with _run_with_follow
    *,
    executor: Any,  # noqa: ANN401
    store: Any,  # noqa: ANN401
    run_id: Any,  # noqa: ANN401
    fmt: str,
    out: TextIO,
    err: TextIO,
) -> int:
    """Default mode — block until terminal; print final outputs.

    Exit 0 on ``succeeded``; 1 on ``failed`` / ``timed_out`` /
    ``aborted``. The JSONL has the authoritative event log; we
    extract the terminal event's payload here for the user-visible
    summary.
    """
    from workflows.run import WorkflowRunStatus  # noqa: PLC0415

    status = await executor.run()
    payload = _terminal_payload(store, run_id)
    if status == WorkflowRunStatus.SUCCEEDED:
        if fmt == "json":
            out.write(
                render(
                    command="workflows run",
                    fmt=fmt,
                    data={
                        "run_id": str(run_id),
                        "status": status.value,
                        "outputs": payload.get("outputs", {}),
                    },
                )
            )
            out.write("\n")
        else:
            out.write(f"run {run_id} {status.value}\n")
            outputs = payload.get("outputs") or {}
            if outputs:
                import json as _json  # noqa: PLC0415

                out.write(
                    _json.dumps(outputs, sort_keys=True, default=str) + "\n"
                )
        return 0
    # Non-success terminal.
    reason = payload.get("reason", "(no reason recorded)")
    if fmt == "json":
        out.write(
            render(
                command="workflows run",
                fmt=fmt,
                status="error",
                errors=[
                    {
                        "class": "WorkflowRunFailed",
                        "summary": (
                            f"run {run_id} ended {status.value}: {reason}"
                        ),
                    }
                ],
            )
        )
        out.write("\n")
    else:
        err.write(f"run {run_id} {status.value}: {reason}\n")
    return 1


async def _run_with_follow(  # noqa: PLR0913 — six explicit kw args; shared with _run_blocking
    *,
    executor: Any,  # noqa: ANN401
    store: Any,  # noqa: ANN401
    run_id: Any,  # noqa: ANN401
    fmt: str,
    out: TextIO,
    err: TextIO,
) -> int:
    """``--follow`` mode — concurrent executor + JSONL tail printer.

    The executor coroutine runs as a task; the tail iterator polls
    the JSONL and prints one JSON line per event. Termination is
    driven by the tail: it stops yielding once a terminal
    ``workflow.run.*`` event lands. Then we await the executor for
    its terminal status.
    """
    import asyncio  # noqa: PLC0415

    from workflows.run import WorkflowRunStatus  # noqa: PLC0415

    exec_task = asyncio.create_task(executor.run())
    try:
        await asyncio.to_thread(_drain_tail_to_writer, store, run_id, out)
    finally:
        # If the tail printer raised, still let the executor finish so
        # the JSONL terminates cleanly.
        status: WorkflowRunStatus = await exec_task
    if status == WorkflowRunStatus.SUCCEEDED:
        return 0
    payload = _terminal_payload(store, run_id)
    if fmt != "json":
        reason = payload.get("reason", "(no reason recorded)")
        err.write(f"run {run_id} {status.value}: {reason}\n")
    # Surface a non-zero exit so shell pipelines reflect failure.
    return 1


def _drain_tail_to_writer(store: Any, run_id: Any, out: TextIO) -> None:  # noqa: ANN401
    """Synchronous-thread JSONL tail printer.

    Runs inside :func:`asyncio.to_thread` so the ``time.sleep`` polls
    in :meth:`WorkflowRunStore.tail` don't block the event loop.
    """
    import json as _json  # noqa: PLC0415

    for event in store.tail(run_id):
        out.write(_json.dumps(event, sort_keys=True, default=str))
        out.write("\n")
        out.flush()


def _terminal_payload(store: Any, run_id: Any) -> dict[str, Any]:  # noqa: ANN401
    """Read the run's last ``workflow.run.*`` event and return its payload."""
    terminal_kinds = {
        "workflow.run.completed",
        "workflow.run.failed",
        "workflow.run.timed_out",
        "workflow.run.aborted",
    }
    last: dict[str, Any] = {}
    for event in store.read_events(run_id):
        if event.get("kind") in terminal_kinds:
            last = event.get("payload") or {}
    return last


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
