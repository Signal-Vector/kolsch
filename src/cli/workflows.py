"""`kolsch workflows` CLI surface.

Eight subcommands:

- `list` — print catalog entries with `asset_type="workflow"`.
- `show <ref>` — print parsed-workflow structure.
- `validate <ref>` — run the validation gate via
  [`validate_workflow_or_raise`][workflows.validator.validate_workflow_or_raise].
- `cancel <run_id>` — atomically write a sidecar `cancel.flag`
  into the run directory.
- `run <ref>` — execute a workflow end-to-end.
- `resume <run_id>` — resume a `waiting_for_input` run with
  `--input response=<text>`.
- `events <run_id>` — list the run's `events.jsonl` entries so
  operators can discover `event_id` candidates for
  `workflows fork --at <event_id>`.
- `fork <run_id> --at <event_id>` — create a forked run rooted at
  `<event_id>` of `<run_id>`'s JSONL; replays the parent's
  pre-fork prefix, then executes downstream. `--input k=v`
  overrides patch the fork-point node's captured output.

All commands accept `--format text|json`. Text/json branching
happens exclusively inside
`render` (private) — no subcommand prints payload
directly.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, Any, TextIO

from assets import AssetManagement
from cli._format import render, render_exception
from workflows._args import parse_input_pairs
from workflows.errors import (
    WorkflowError,
    WorkflowRunNotFound,
    WorkflowValidationError,
)
from workflows.parser import load_workflow
from workflows.validator import validate_workflow_or_raise

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["add_workflows_subparser", "cmd_workflows"]


# ---------------------------------------------------------------------------
# Subparser registration.
# ---------------------------------------------------------------------------


def _build_format_parent() -> argparse.ArgumentParser:
    """Tiny parent parser that defines `--format` once.

    `--format` must be accepted at both the group level
    (`workflows --format json list`) and the subcommand level
    (`workflows list --format json`). Argparse subparsers do not
    inherit parent flags by default; the canonical fix is to
    define the flag on a `parents=[…]` parser shared by every
    parser in the tree. `add_help=False` keeps the helper from
    injecting a second `-h`.
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
    """Register the `kolsch workflows` subparser tree."""
    fmt_parent = _build_format_parent()
    workflows: argparse.ArgumentParser = sub.add_parser(
        "workflows",
        help=(
            "Manage workflow assets "
            "(list, show, validate, cancel, run, resume, events, fork)."
        ),
        parents=[fmt_parent],
    )
    workflows_sub = workflows.add_subparsers(dest="workflows_command", metavar="<op>")

    workflows_sub.add_parser(
        "list", help="List catalogued workflows.", parents=[fmt_parent]
    )

    show = workflows_sub.add_parser(
        "show",
        help="Print a workflow's parsed structure.",
        parents=[fmt_parent],
    )
    show.add_argument("ref", help="Catalog ref (e.g. kolsch/workflows/hello).")

    validate = workflows_sub.add_parser(
        "validate",
        help="Run the Step 5 run-start validation gate.",
        parents=[fmt_parent],
    )
    validate.add_argument("ref", help="Catalog ref.")

    cancel = workflows_sub.add_parser(
        "cancel",
        help=("Request cancellation by writing a sidecar cancel.flag."),
        parents=[fmt_parent],
    )
    cancel.add_argument("run_id", help="Workflow run id.")

    run = workflows_sub.add_parser(
        "run",
        help="Execute a workflow end-to-end.",
        parents=[fmt_parent],
    )
    run.add_argument("ref", help="Catalog ref (e.g. kolsch/workflows/hello).")
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

    # `kolsch workflows resume <run_id> --input response=<text>`.
    # Reuses `_input_pairs_or_exit` (the same KEY=VALUE grammar
    # `run` consumes). The CLI gates on the run's status: rejects
    # with exit 1 + `WorkflowResumeRejected` if the run isn't
    # `waiting_for_input`.
    resume = workflows_sub.add_parser(
        "resume",
        help="Resume a paused workflow run with the user's response.",
        parents=[fmt_parent],
    )
    resume.add_argument(
        "run_id", help="Workflow run id (wrk_*) in waiting_for_input state."
    )
    resume.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "Resume input pair; the user_input runner's output schema "
            "is ``{response: str}`` so the canonical shape is "
            "``--input response=<text>``. Repeatable."
        ),
    )

    # `kolsch workflows events <run_id>`: list the JSONL events
    # for a run so operators can discover candidate `event_id`
    # values to pass to `workflows fork --at <event_id>`. Text +
    # json formats both supported.
    events = workflows_sub.add_parser(
        "events",
        help="List events in a workflow run's events.jsonl.",
        parents=[fmt_parent],
    )
    events.add_argument("run_id", help="Workflow run id (wrk_*) whose events to list.")

    # `kolsch workflows fork <run_id> --at <event_id> [--input
    # k=v]...`. The `--help` description explicitly notes the
    # side-effect discipline: upstream side effects produced by
    # the parent's pre-fork prefix (bash writes, sub-workflow
    # runs, external API calls) are NOT undone by fork. Operators
    # rely on this guidance for non-trivial pipelines.
    fork_p = workflows_sub.add_parser(
        "fork",
        help=("Fork a workflow run from a specific event (--at <event_id>)."),
        description=(
            "Create a forked workflow run rooted at <event_id> of "
            "<parent_run_id>'s events.jsonl. The forked run replays "
            "every parent event up to (and including) the fork point, "
            "then executes from there. Use 'kolsch workflows events "
            "<run_id>' to discover candidate event ids.\n\n"
            "Side-effect discipline: upstream side effects produced "
            "by the parent's pre-fork prefix are NOT undone. A bash "
            "node that wrote a file or a sub-workflow that called an "
            "external service stays as it was — the fork starts with "
            "the world in whatever state the parent left it. Choose "
            "a fork point whose downstream is safe to re-execute "
            "against that state."
        ),
        parents=[fmt_parent],
    )
    fork_p.add_argument("run_id", help="Parent workflow run id (wrk_*) to fork from.")
    fork_p.add_argument(
        "--at",
        required=True,
        dest="fork_event_id",
        metavar="EVENT_ID",
        help=(
            "Event id (ev_*) in the parent's events.jsonl at which to fork. Required."
        ),
    )
    fork_p.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "Override the fork-point node's captured output (or "
            "workflow inputs when forking at workflow.run.started). "
            "Repeatable. Other fork-event kinds reject --input."
        ),
    )

    return workflows


# ---------------------------------------------------------------------------
# Top-level dispatcher.
# ---------------------------------------------------------------------------


# PLR0911 acknowledged: the dispatcher has one return per leaf
# subcommand (6) plus the no-op and unknown-op error paths. Each
# return tunnels the leaf's own exit code; collapsing into a mapping
# would lose the type-checker visibility of each leaf's signature
# differences (args.ref vs args.run_id vs no positional).
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
            "ops: list, show, validate, cancel, run, resume, events, fork\n"
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
    if op == "resume":
        return await _cmd_resume(
            root=args.root,
            run_id_str=args.run_id,
            input_pairs=list(getattr(args, "input", None) or []),
            fmt=fmt,
            out=out,
            err=err,
        )
    if op == "events":
        return _cmd_events(
            root=args.root,
            run_id_str=args.run_id,
            fmt=fmt,
            out=out,
            err=err,
        )
    if op == "fork":
        return await _cmd_fork(
            root=args.root,
            parent_run_id_str=args.run_id,
            fork_event_id=args.fork_event_id,
            input_pairs=list(getattr(args, "input", None) or []),
            fmt=fmt,
            out=out,
            err=err,
        )

    err.write(f"unknown workflows op {op!r}\n")
    return 1


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _cmd_list(catalog: Any, *, fmt: str, out: TextIO) -> int:
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
    catalog: Any,
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
    catalog: Any,
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
        sub_errors = [exc for exc in group.exceptions if isinstance(exc, WorkflowError)]
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
# cancel
# ---------------------------------------------------------------------------


def _cmd_cancel(
    root: Path,
    run_id: str,
    *,
    fmt: str,
    out: TextIO,
    err: TextIO,
) -> int:
    """Atomically write `<workspace>/runs/<run_id>/cancel.flag`.

    The executor polls this file between node boundaries; engine
    startup runs an orphan sweep that reconciles `status=running`
    runs without a live PID. The CLI does not signal any process
    and does not check executor liveness — write the flag and exit.
    """
    runs_dir = root / "runs"
    run_dir = runs_dir / run_id
    if not run_dir.is_dir():
        msg = f"run {run_id!r} not found under {runs_dir}"
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
        f"cancel requested for {run_id}; executor will halt at the next node boundary"
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
# run — live executor wire-up
# ---------------------------------------------------------------------------


# PLR0913 acknowledged: the live ``run`` command surface is wide on
# purpose — ref, --input, --dry-run, --follow, --format, plus the
# captured out/err streams. Collapsing into an args object would only
# shift the parameter count behind another constructor; the dispatcher
# already passes them by keyword.
async def _cmd_run(
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
    """Live `kolsch workflows run <ref>` handler.

    Three modes:

    - `--dry-run` prints the resolved topological plan + per-node
      raw `binds` preview and exits 0 without touching the
      executor (no JSONL written, no module imports).
    - `--follow` runs the executor concurrently with a JSONL tail
      that prints one line per event until the terminal event
      lands.
    - Default blocks until terminal; prints the final outputs JSON
      on success or the error envelope on failure.
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

    inputs = _input_pairs_or_exit(input_pairs)

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

    # Live executor wiring. Factored into
    # `cli._workflow_runtime.build_workflow_executor` so the chat
    # `/workflow` slash handler shares this exact wiring.
    from cli._workflow_runtime import (  # noqa: PLC0415
        build_workflow_executor,
    )
    from workflows.run_store import WorkflowRunStore  # noqa: PLC0415

    store = WorkflowRunStore(root)
    run = store.create(workflow_ref=entry.ref, inputs=inputs)
    executor = build_workflow_executor(
        root=root,
        runtime=runtime,
        workflow=workflow,
        run=run,
        store=store,
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


# ---------------------------------------------------------------------------
# resume — CLI for paused-run continuation
# ---------------------------------------------------------------------------


async def _cmd_resume(
    *,
    root: Path,
    run_id_str: str,
    input_pairs: list[str],
    fmt: str,
    out: TextIO,
    err: TextIO,
) -> int:
    """Live `kolsch workflows resume <run_id>` handler.

    Loads the paused run, builds an executor pointed at the same
    workflow + store, awaits `IterativeExecutor.resume` with the
    parsed response dict, and renders the terminal envelope. The
    status-gate exception (`WorkflowResumeRejected`) and bind
    validation (`WorkflowInputInvalid`) both surface as exit 1 +
    the standard error envelope; an actual resume that reaches
    terminal status exits 0 on `succeeded` and 1 on `failed` /
    `aborted` / `timed_out`. A re-pause (`waiting_for_input`
    returned from `resume` because the workflow has another
    `user_input` node downstream) is not a failure — exit 0 with
    a "resume again with..." hint.
    """
    from pathlib import Path as _Path  # noqa: PLC0415

    from platform_runtime import bootstrap_runtime  # noqa: PLC0415

    response = _input_pairs_or_exit(input_pairs)

    runtime = await bootstrap_runtime(root=root)
    catalog = runtime.asset_management.catalog()

    from workflows.ids import WorkflowRunId  # noqa: PLC0415
    from workflows.run_store import WorkflowRunStore  # noqa: PLC0415

    store = WorkflowRunStore(root)
    try:
        run = store.read(WorkflowRunId(run_id_str))
    except (FileNotFoundError, ValueError):
        # ``ValueError`` covers a malformed ``wrk_<not-a-uuid>``
        # surface (``WorkflowRunId`` validates the UUID body). Both
        # shapes are user-input errors; surface the same
        # "run not found" envelope.
        return _emit_run_not_found(
            command="workflows resume",
            run_id=run_id_str,
            fmt=fmt,
            out=out,
            err=err,
        )

    entry = catalog.lookup(run.workflow_ref, expected_type="workflow")
    if entry is None:
        return _emit_unknown_ref(
            command="workflows resume",
            ref=run.workflow_ref,
            fmt=fmt,
            out=out,
            err=err,
        )
    try:
        workflow = load_workflow(_Path(entry.path))
    except WorkflowError as exc:
        return _emit_error_envelope(
            command="workflows resume",
            errors=[exc],
            fmt=fmt,
            out=out,
            err=err,
        )

    # Live executor wiring — mirrors `_cmd_run` so the resume
    # process has the same builtin runner set (the resume may
    # continue into bash / agent / catalog-node nodes downstream
    # of the user_input).
    from tools.builtins import (  # noqa: PLC0415
        make_bash,
        make_browse_url,
        make_list_files,
        make_read_file,
        make_write_file,
    )
    from tools.decorator import get_tool_definition  # noqa: PLC0415
    from workflows.builtins import register_builtins  # noqa: PLC0415
    from workflows.errors import (  # noqa: PLC0415
        WorkflowInputInvalid,
        WorkflowResumeRejected,
    )
    from workflows.executor import (  # noqa: PLC0415
        IterativeExecutor,
        RunnerRegistry,
    )

    bash_fn = make_bash(policy=runtime.path_policy, root=root)
    bash_defn = get_tool_definition(bash_fn)
    # Mirror `_cmd_run`: resumes downstream of `user_input` may
    # hit file-IO nodes.
    read_file_defn = get_tool_definition(
        make_read_file(policy=runtime.path_policy, root=root)
    )
    write_file_defn = get_tool_definition(
        make_write_file(policy=runtime.path_policy, root=root)
    )
    list_files_defn = get_tool_definition(
        make_list_files(policy=runtime.path_policy, root=root)
    )
    browse_url_defn = get_tool_definition(
        make_browse_url(policy=runtime.path_policy, root=root)
    )
    registry = RunnerRegistry()

    # Same factory wiring as `_cmd_run` so a resumed workflow that
    # fires a downstream sub-workflow node can still construct
    # nested executors.
    def _executor_factory(**kwargs: Any) -> IterativeExecutor:
        return IterativeExecutor(**kwargs)

    register_builtins(
        registry,
        bash_defn=bash_defn,
        authz=runtime.authz,
        secrets=runtime.secrets,
        sandbox=runtime.sandbox,
        catalog=catalog,
        engine=runtime.engine,
        session_store=runtime.store,
        config_builder=runtime.build_resolved_config,
        read_file_defn=read_file_defn,
        write_file_defn=write_file_defn,
        list_files_defn=list_files_defn,
        browse_url_defn=browse_url_defn,
        workflow_store=store,
        executor_factory=_executor_factory,
    )
    executor = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=catalog,
    )

    try:
        status = await executor.resume(response=response)
    except (WorkflowResumeRejected, WorkflowInputInvalid) as exc:
        return _emit_error_envelope(
            command="workflows resume",
            errors=[exc],
            fmt=fmt,
            out=out,
            err=err,
        )

    return _render_terminal_for_command(
        command="workflows resume",
        store=store,
        run_id=run.run_id,
        status=status,
        fmt=fmt,
        out=out,
        err=err,
    )


# ---------------------------------------------------------------------------
# fork — CLI handler
# ---------------------------------------------------------------------------


async def _cmd_fork(
    *,
    root: Path,
    parent_run_id_str: str,
    fork_event_id: str,
    input_pairs: list[str],
    fmt: str,
    out: TextIO,
    err: TextIO,
) -> int:
    """Live `kolsch workflows fork <run_id> --at <event_id>` handler.

    Mirrors `_cmd_resume`'s shape: load the parent run, build an
    executor pointed at the same workflow + store, mint a forked
    `WorkflowRun` whose `forked_from` pointer encodes the fork
    target, then await `IterativeExecutor.start_from_fork` with
    the parsed `--input` overrides.

    Sub-workflow fork semantics are handled transitively by the
    executor's replay: forking AFTER a workflow-call node
    rehydrates the captured child output (no new child run
    created); forking BEFORE the workflow-call node causes the
    forked run's worklist to fire the workflow node fresh
    (spawning a new child). No special-case code lives here.

    Success envelope: `{new_run_id, parent_run_id, fork_event_id,
    status}`. JSON exit code matches the terminal status: 0 for
    `succeeded` and `waiting_for_input`, 1 for every failure
    shape.
    """
    from pathlib import Path as _Path  # noqa: PLC0415

    from platform_runtime import bootstrap_runtime  # noqa: PLC0415

    overrides = _input_pairs_or_exit(input_pairs)

    runtime = await bootstrap_runtime(root=root)
    catalog = runtime.asset_management.catalog()

    from schemas import EventId  # noqa: PLC0415
    from schemas.entities import ForkedFrom  # noqa: PLC0415
    from workflows.errors import (  # noqa: PLC0415
        WorkflowInputInvalid,
        WorkflowResumeRejected,
        WorkflowRuntimeError,
    )
    from workflows.ids import WorkflowRunId  # noqa: PLC0415
    from workflows.run_store import WorkflowRunStore  # noqa: PLC0415

    store = WorkflowRunStore(root)
    try:
        parent_run = store.read(WorkflowRunId(parent_run_id_str))
    except (FileNotFoundError, ValueError):
        # ValueError covers a malformed ``wrk_<not-a-uuid>``; both
        # surfaces are operator input errors. Same envelope as
        # :func:`_cmd_resume`.
        return _emit_error_envelope(
            command="workflows fork",
            errors=[WorkflowRunNotFound(run_id=parent_run_id_str)],
            fmt=fmt,
            out=out,
            err=err,
        )

    entry = catalog.lookup(parent_run.workflow_ref, expected_type="workflow")
    if entry is None:
        return _emit_unknown_ref(
            command="workflows fork",
            ref=parent_run.workflow_ref,
            fmt=fmt,
            out=out,
            err=err,
        )
    try:
        workflow = load_workflow(_Path(entry.path))
    except WorkflowError as exc:
        return _emit_error_envelope(
            command="workflows fork",
            errors=[exc],
            fmt=fmt,
            out=out,
            err=err,
        )

    # Mint the forked run BEFORE we discover whether the fork point
    # is valid: a forked-but-empty run is a clean state for the
    # operator to retry against (the existing meta.json captures the
    # forked_from pointer; the JSONL is empty). The status flip and
    # the first event emit only happen inside start_from_fork.
    forked_run = store.create(
        workflow_ref=parent_run.workflow_ref,
        inputs=dict(parent_run.inputs),
        trace_id=parent_run.trace_id,
        forked_from=ForkedFrom(
            run_id=parent_run.run_id,
            fork_event_id=EventId(fork_event_id),
        ),
    )

    # Live executor wiring — mirrors `_cmd_run` / `_cmd_resume` so
    # the fork process has the same builtin runner set. A fork
    # downstream of the fork point may hit any node type the
    # workflow uses (bash, agent, sub-workflow, file IO, state,
    # branching).
    from tools.builtins import (  # noqa: PLC0415
        make_bash,
        make_browse_url,
        make_list_files,
        make_read_file,
        make_write_file,
    )
    from tools.decorator import get_tool_definition  # noqa: PLC0415
    from workflows.builtins import register_builtins  # noqa: PLC0415
    from workflows.executor import (  # noqa: PLC0415
        IterativeExecutor,
        RunnerRegistry,
    )

    bash_fn = make_bash(policy=runtime.path_policy, root=root)
    bash_defn = get_tool_definition(bash_fn)
    read_file_defn = get_tool_definition(
        make_read_file(policy=runtime.path_policy, root=root)
    )
    write_file_defn = get_tool_definition(
        make_write_file(policy=runtime.path_policy, root=root)
    )
    list_files_defn = get_tool_definition(
        make_list_files(policy=runtime.path_policy, root=root)
    )
    browse_url_defn = get_tool_definition(
        make_browse_url(policy=runtime.path_policy, root=root)
    )
    registry = RunnerRegistry()

    def _executor_factory(**kwargs: Any) -> IterativeExecutor:
        return IterativeExecutor(**kwargs)

    register_builtins(
        registry,
        bash_defn=bash_defn,
        authz=runtime.authz,
        secrets=runtime.secrets,
        sandbox=runtime.sandbox,
        catalog=catalog,
        engine=runtime.engine,
        session_store=runtime.store,
        config_builder=runtime.build_resolved_config,
        read_file_defn=read_file_defn,
        write_file_defn=write_file_defn,
        list_files_defn=list_files_defn,
        browse_url_defn=browse_url_defn,
        workflow_store=store,
        executor_factory=_executor_factory,
    )
    executor = IterativeExecutor(
        workflow=workflow,
        run=forked_run,
        store=store,
        registry=registry,
        catalog=catalog,
    )

    try:
        status = await executor.start_from_fork(
            fork_event_id=fork_event_id,
            input_overrides=overrides,
        )
    except WorkflowRunNotFound as exc:
        return _emit_error_envelope(
            command="workflows fork",
            errors=[exc],
            fmt=fmt,
            out=out,
            err=err,
        )
    except (
        WorkflowRuntimeError,
        WorkflowInputInvalid,
        WorkflowResumeRejected,
        WorkflowError,
    ) as exc:
        # Render every fork-time error through the same envelope
        # so operators see `WorkflowRuntimeError` /
        # `WorkflowInputInvalid` / etc. uniformly. `WorkflowError`
        # is the catchall — keep it last so the more-specific arms
        # still bind first.
        return _emit_error_envelope(
            command="workflows fork",
            errors=[exc],
            fmt=fmt,
            out=out,
            err=err,
        )

    return _render_fork_terminal(
        store=store,
        forked_run_id=forked_run.run_id,
        parent_run_id=parent_run.run_id,
        fork_event_id=fork_event_id,
        status=status,
        fmt=fmt,
        out=out,
        err=err,
    )


def _render_fork_terminal(
    *,
    store: Any,
    forked_run_id: Any,
    parent_run_id: Any,
    fork_event_id: str,
    status: Any,
    fmt: str,
    out: TextIO,
    err: TextIO,
) -> int:
    """Render the post-fork envelope.

    Differs from `_render_terminal_for_command` by also surfacing
    the parent run id + fork event id so operators can correlate
    the fork without re-reading meta.json. Same exit-code
    discipline:

    - `succeeded` → 0 with the success envelope + final outputs.
    - `waiting_for_input` → 0 with the `resume_with` hint; a
      forked run can pause downstream of its fork point on a
      user_input node.
    - terminal failure → 1 with the error envelope.
    """
    from workflows.run import WorkflowRunStatus  # noqa: PLC0415

    payload = _terminal_payload(store, forked_run_id)
    base_data: dict[str, Any] = {
        "new_run_id": str(forked_run_id),
        "parent_run_id": str(parent_run_id),
        "fork_event_id": fork_event_id,
        "status": status.value,
    }

    if status == WorkflowRunStatus.SUCCEEDED:
        if fmt == "json":
            out.write(
                render(
                    command="workflows fork",
                    fmt=fmt,
                    data={
                        **base_data,
                        "outputs": payload.get("outputs", {}),
                    },
                )
            )
            out.write("\n")
        else:
            out.write(f"forked run {forked_run_id} {status.value}\n")
            out.write(f"forked from: {parent_run_id} at {fork_event_id}\n")
            outputs = payload.get("outputs") or {}
            if outputs:
                import json as _json  # noqa: PLC0415

                out.write(_json.dumps(outputs, sort_keys=True, default=str) + "\n")
        return 0

    if status == WorkflowRunStatus.WAITING_FOR_INPUT:
        hint = f"kolsch workflows resume {forked_run_id} --input response=<text>"
        if fmt == "json":
            out.write(
                render(
                    command="workflows fork",
                    fmt=fmt,
                    data={**base_data, "resume_with": hint},
                )
            )
            out.write("\n")
        else:
            out.write(f"forked run {forked_run_id} {status.value}\n")
            out.write(f"forked from: {parent_run_id} at {fork_event_id}\n")
            out.write(f"resume with: {hint}\n")
        return 0

    # Failed / aborted / timed_out / (defensively) pending / running.
    reason = payload.get("reason", "(no reason recorded)")
    if fmt == "json":
        out.write(
            render(
                command="workflows fork",
                fmt=fmt,
                status="error",
                errors=[
                    {
                        "class": "WorkflowRunFailed",
                        "summary": (
                            f"forked run {forked_run_id} ended {status.value}: {reason}"
                        ),
                    }
                ],
            )
        )
        out.write("\n")
    else:
        err.write(f"forked run {forked_run_id} {status.value}: {reason}\n")
    return 1


# ---------------------------------------------------------------------------
# events — CLI for listing a run's JSONL events
# ---------------------------------------------------------------------------


# How wide to render the payload preview column. Wider would push the
# table past one terminal line for typical 120-col operators; narrower
# would truncate meaningful identifiers like ``response='...'``.
_EVENT_PAYLOAD_PREVIEW_WIDTH = 50


def _cmd_events(
    *,
    root: Path,
    run_id_str: str,
    fmt: str,
    out: TextIO,
    err: TextIO,
) -> int:
    """`kolsch workflows events <run_id>` handler.

    Lists every JSONL event for `run_id` with its `event_id`,
    `kind`, `ts`, and a brief payload preview. Operators consume
    this to discover candidate `event_id` values for `workflows
    fork --at <event_id>`.

    A run with an empty `events.jsonl` (e.g. freshly minted, never
    executed) returns an empty list cleanly — that's an honest
    state, not an error.
    """
    from workflows.ids import WorkflowRunId  # noqa: PLC0415
    from workflows.run_store import WorkflowRunStore  # noqa: PLC0415

    store = WorkflowRunStore(root)
    try:
        # ``read`` confirms the run is real before we attempt to walk
        # its JSONL; without this gate, a missing run would silently
        # yield an empty event list.
        store.read(WorkflowRunId(run_id_str))
    except (FileNotFoundError, ValueError):
        return _emit_error_envelope(
            command="workflows events",
            errors=[WorkflowRunNotFound(run_id=run_id_str)],
            fmt=fmt,
            out=out,
            err=err,
        )

    rows = [
        {
            "event_id": event.get("event_id", ""),
            "kind": event.get("kind", ""),
            "ts": event.get("ts", ""),
            "payload_preview": _preview_payload(event.get("payload", {})),
        }
        for event in store.read_events(WorkflowRunId(run_id_str))
    ]

    if fmt == "json":
        out.write(
            render(
                command="workflows events",
                fmt=fmt,
                data={"run_id": run_id_str, "events": rows},
            )
        )
        out.write("\n")
        return 0

    if not rows:
        out.write(
            render(
                command="workflows events",
                fmt=fmt,
                text_body=f"(no events for run {run_id_str})\n",
            )
        )
        return 0

    lines = ["event_id\tkind\tts\tpayload"]
    for row in rows:
        lines.append(
            f"{row['event_id']}\t{row['kind']}\t{row['ts']}\t{row['payload_preview']}"
        )
    out.write(
        render(
            command="workflows events",
            fmt=fmt,
            text_body="\n".join(lines) + "\n",
        )
    )
    return 0


def _preview_payload(payload: Any) -> str:
    """Render a single-line, length-bounded preview of a payload dict.

    Operators scan this column to disambiguate near-identical events
    (e.g., two ``workflow.node.completed`` rows for different nodes).
    The format is a deliberately compact ``k=v, k=v`` rendering; any
    value longer than ``_EVENT_PAYLOAD_PREVIEW_WIDTH`` is truncated
    with a trailing ellipsis so the whole row stays scannable.
    """
    if not isinstance(payload, dict) or not payload:
        return ""
    parts: list[str] = []
    for key, value in payload.items():
        rendered = _shorten(str(value), _EVENT_PAYLOAD_PREVIEW_WIDTH)
        parts.append(f"{key}={rendered}")
    preview = ", ".join(parts)
    return _shorten(preview, _EVENT_PAYLOAD_PREVIEW_WIDTH)


def _shorten(text: str, width: int) -> str:
    """Truncate ``text`` to ``width`` chars with a trailing ellipsis.

    Newlines and tabs in payload values would corrupt the table layout;
    collapse them to spaces before truncating so a multi-line value
    renders as one row.
    """
    flattened = text.replace("\n", " ").replace("\t", " ")
    if len(flattened) <= width:
        return flattened
    if width <= 3:
        return flattened[:width]
    return flattened[: width - 3] + "..."


def _input_pairs_or_exit(pairs: list[str]) -> dict[str, Any]:
    """Wrap `parse_input_pairs` so malformed `--input` is a clean exit.

    The shared
    [`parse_input_pairs`][workflows._args.parse_input_pairs]
    raises `ValueError`; the CLI's contract is a hard exit with a
    clear message, preserved here.
    """
    try:
        return parse_input_pairs(pairs)
    except ValueError as exc:
        # The shared parser uses the more generic "input pair" phrasing;
        # the CLI flag is ``--input``, so rewrite for the user's surface.
        msg = str(exc).replace("input pair must be", "--input must be")
        raise SystemExit(msg) from exc


def _compute_dry_run_topology(workflow: Any) -> list[str]:
    """Kahn's algorithm replicated here for the dry-run path.

    Duplicates
    `IterativeExecutor._compute_topology` so the dry-run never
    imports the executor module (which would in turn load
    `workflows.module_isolation` + bundle plumbing).
    """
    from collections import deque  # noqa: PLC0415

    node_names = list(workflow.nodes)
    edges = list(workflow.graph.edges) if workflow.graph is not None else []
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


def _emit_dry_run_plan(
    *,
    workflow: Any,
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
            "type": getattr(workflow.nodes[name], "type", "<copy_of>"),
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


def _node_binds_preview(node_def: Any) -> dict[str, Any]:
    """Return the node's ``binds`` mapping in its raw, unrendered form.

    Jinja-marked values (``"{{ ... }}"``) are reported verbatim so the
    operator can see exactly what would render at runtime.
    """
    binds = getattr(node_def, "binds", None) or {}
    if isinstance(binds, dict):
        return dict(binds)
    return {}


async def _run_blocking(
    *,
    executor: Any,
    store: Any,
    run_id: Any,
    fmt: str,
    out: TextIO,
    err: TextIO,
) -> int:
    """Default mode — block until terminal or pause; render the result.

    Delegates rendering to `_render_terminal_for_command` so the
    same envelope shape is shared with `_cmd_resume`. The
    `waiting_for_input` status returned from
    `IterativeExecutor.run` (initial pause) is rendered as exit 0
    + a "resume with..." hint so the operator's terminal pipeline
    reflects success-of-pause.
    """
    status = await executor.run()
    return _render_terminal_for_command(
        command="workflows run",
        store=store,
        run_id=run_id,
        status=status,
        fmt=fmt,
        out=out,
        err=err,
    )


def _render_terminal_for_command(
    *,
    command: str,
    store: Any,
    run_id: Any,
    status: Any,
    fmt: str,
    out: TextIO,
    err: TextIO,
) -> int:
    """Render the post-executor envelope for `run` / `resume`.

    Four shapes:

    - `succeeded` → exit 0 + outputs (JSON or text).
    - `waiting_for_input` → exit 0 + "resume with ..." hint. A
      pause is a legitimate non-terminal outcome for the CLI;
      treating it as failure would force the operator to read the
      run id from a non-zero pipeline.
    - `failed` / `timed_out` / `aborted` → exit 1 + error
      envelope; `reason` lifted from the terminal event payload.
    - `pending` / `running` (defensive) → exit 1 + unknown-state
      envelope; should never happen in practice.
    """
    from workflows.run import WorkflowRunStatus  # noqa: PLC0415

    payload = _terminal_payload(store, run_id)
    mode, peak = _concurrency_envelope(store, run_id)
    if status == WorkflowRunStatus.SUCCEEDED:
        if fmt == "json":
            out.write(
                render(
                    command=command,
                    fmt=fmt,
                    data={
                        "run_id": str(run_id),
                        "status": status.value,
                        "outputs": payload.get("outputs", {}),
                        "concurrency_mode": mode,
                        "peak_in_flight": peak,
                    },
                )
            )
            out.write("\n")
        else:
            out.write(f"run {run_id} {status.value}\n")
            outputs = payload.get("outputs") or {}
            if outputs:
                import json as _json  # noqa: PLC0415

                out.write(_json.dumps(outputs, sort_keys=True, default=str) + "\n")
            out.write(f"concurrency_mode: {mode}\n")
            out.write(f"peak_in_flight: {peak}\n")
        return 0
    if status == WorkflowRunStatus.WAITING_FOR_INPUT:
        hint = f"kolsch workflows resume {run_id} --input response=<text>"
        if fmt == "json":
            out.write(
                render(
                    command=command,
                    fmt=fmt,
                    data={
                        "run_id": str(run_id),
                        "status": status.value,
                        "resume_with": hint,
                    },
                )
            )
            out.write("\n")
        else:
            out.write(f"run {run_id} {status.value}\n")
            out.write(f"resume with: {hint}\n")
        return 0
    # Failed / aborted / timed_out terminal.
    reason = payload.get("reason", "(no reason recorded)")
    if fmt == "json":
        out.write(
            render(
                command=command,
                fmt=fmt,
                status="error",
                errors=[
                    {
                        "class": "WorkflowRunFailed",
                        "summary": (f"run {run_id} ended {status.value}: {reason}"),
                    }
                ],
            )
        )
        out.write("\n")
    else:
        err.write(f"run {run_id} {status.value}: {reason}\n")
    return 1


async def _run_with_follow(
    *,
    executor: Any,
    store: Any,
    run_id: Any,
    fmt: str,
    out: TextIO,
    err: TextIO,
) -> int:
    """`--follow` mode — concurrent executor + JSONL tail printer.

    The executor coroutine runs as a task; the tail iterator polls
    the JSONL and prints one JSON line per event. Termination is
    driven by the tail: it stops yielding once a terminal
    `workflow.run.*` event lands. Then await the executor for its
    terminal status.
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


def _drain_tail_to_writer(store: Any, run_id: Any, out: TextIO) -> None:
    """Synchronous-thread JSONL tail printer.

    Runs inside `asyncio.to_thread` so the `time.sleep` polls in
    `WorkflowRunStore.tail` don't block the event loop.
    """
    import json as _json  # noqa: PLC0415

    for event in store.tail(run_id):
        out.write(_json.dumps(event, sort_keys=True, default=str))
        out.write("\n")
        out.flush()


def _concurrency_envelope(store: Any, run_id: Any) -> tuple[str, int]:
    """Surface `concurrency_mode` + `peak_in_flight`.

    Defaults to `("sequential", 1)` so older runs (and
    `sequential`-mode runs which omit both fields for byte-identity)
    render the expected sequential envelope row.
    """
    mode = "sequential"
    peak = 1
    try:
        events = list(store.read_events(run_id))
    except FileNotFoundError:
        return mode, peak
    for event in events:
        payload = event.get("payload") or {}
        kind = event.get("kind")
        if kind == "workflow.run.started":
            field = payload.get("concurrency_mode")
            if isinstance(field, str):
                mode = field
        elif kind in (
            "workflow.run.completed",
            "workflow.run.failed",
        ):
            field = payload.get("peak_in_flight")
            if isinstance(field, int) and field > 0:
                peak = field
    return mode, peak


def _terminal_payload(store: Any, run_id: Any) -> dict[str, Any]:
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


def _emit_run_not_found(
    *,
    command: str,
    run_id: str,
    fmt: str,
    out: TextIO,
    err: TextIO,
) -> int:
    """Render the missing-run envelope for `resume`."""
    msg = f"workflow run {run_id!r} not found"
    if fmt == "json":
        out.write(
            render(
                command=command,
                fmt=fmt,
                status="error",
                errors=[
                    {
                        "class": "WorkflowRunNotFound",
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
