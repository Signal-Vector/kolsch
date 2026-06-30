"""`kolsch` CLI dispatcher.

Argparse-based — no external CLI dep. Each subcommand has a small
async handler in this module; the top-level
[`main`][cli.main.main] is the entry point bound to the `kolsch`
console script.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from platform_runtime import (
    bootstrap_runtime,
    resolve_workspace,
    workspace_is_initialized,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from platform_runtime import PlatformRuntime
    from schemas import ResolvedAgentConfig


_log = logging.getLogger("cli")


# Friendly message printed when a non-setup command lands against an
# uninitialised workspace. The `{path}` token is substituted at print
# time; no other formatting. Plain text on stderr, no JSON, no
# traceback.
_MISSING_WORKSPACE_MESSAGE = (
    "No Kolsch workspace at {path}.\n"
    "\n"
    "Run `kolsch setup` to create one, or point KOLSCH_WORKSPACE at an\n"
    "existing workspace.\n"
)

# Exit code for the missing-workspace path — distinct from generic
# error code 1, so shell pipelines can tell them apart.
_EXIT_MISSING_WORKSPACE = 3


def main(argv: Sequence[str] | None = None) -> int:
    """Run the console-script entry point and return the process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    logging.basicConfig(
        level=logging.WARNING if not args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Resolve the workspace once, intercept the missing-workspace case
    # before any subsystem boots so the user gets a friendly message
    # instead of a stack trace. `setup` is the one command exempt from
    # the initialization gate; it's what populates the workspace.
    resolved_root = resolve_workspace(args.root)
    if args.command != "setup" and not workspace_is_initialized(resolved_root):
        sys.stderr.write(_MISSING_WORKSPACE_MESSAGE.format(path=resolved_root))
        return _EXIT_MISSING_WORKSPACE
    args.root = resolved_root

    # Engine-startup orphan sweep. Any `workflows` or `nodes`
    # invocation reconciles `runs/<wrk_id>/` JSONLs whose executor
    # PID is dead, so the run index never reports a stale `running`
    # row from a crashed process. The sweep is a no-op when the
    # workspace has no `runs/` dir yet.
    if args.command in {"workflows", "nodes"}:
        _run_orphan_sweep(args.root)

    handler = _DISPATCH[args.command]
    return asyncio.run(handler(args))


def _run_orphan_sweep(root: Path) -> None:
    """Reconcile orphaned runs; log reconciled IDs at INFO.

    Failures here MUST NOT crash the CLI — a corrupt JSONL or filesystem
    glitch in one run shouldn't block the operator's actual command.
    """
    try:
        from workflows.orphan_sweep import sweep  # noqa: PLC0415
        from workflows.run_store import WorkflowRunStore  # noqa: PLC0415

        store = WorkflowRunStore(root)
        reconciled = sweep(store)
    except Exception:  # noqa: BLE001 — sweep must not crash CLI
        _log.exception("orphan sweep failed; continuing")
        return
    if reconciled:
        _log.info(
            "orphan sweep reconciled %d run(s): %s",
            len(reconciled),
            ", ".join(str(r) for r in reconciled),
        )


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


_PARSER_REGISTRY: dict[str, argparse.ArgumentParser] = {}
"""Stash for subcommand parsers, populated by `_build_parser`.

Lets handlers print sub-parser-specific help when a multi-subcommand
top-level (`sessions`, `permissions`) is invoked without an op.
Keyed by command name (`"sessions"`, `"permissions"`)."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kolsch",
        description="Agent Platform CLI.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help=("Workspace root. Default: $KOLSCH_WORKSPACE or ~/Documents/kolsch."),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable INFO-level logging.",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # ---- run ---------------------------------------------------------
    run = sub.add_parser("run", help="Execute one agent exchange.")
    run.add_argument(
        "agent_ref",
        help="Full agent ref (e.g. acme/researcher).",
    )
    run.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Input key/value pair; repeatable. The first one named "
        "'content' becomes the conversational user message.",
    )
    run.add_argument(
        "--stream",
        action="store_true",
        help="Stream events as they arrive (default: collect + print final).",
    )
    run.add_argument(
        "--session",
        type=str,
        default=None,
        help="Existing session id (default: create a fresh session).",
    )
    run.add_argument(
        "--skill",
        action="append",
        default=[],
        metavar="<bundle>/skills/<name>",
        help=(
            "Force-load a skill before the exchange. Repeatable. The "
            "engine emits a synthetic tool.read on the SKILL.md path. "
            "Refs not in the agent's enabled list fail at submit time."
        ),
    )

    # ---- sessions ----------------------------------------------------
    sessions = sub.add_parser("sessions", help="Session operations.")
    _PARSER_REGISTRY["sessions"] = sessions
    sessions_sub = sessions.add_subparsers(dest="sessions_command", metavar="<op>")

    sessions_list = sessions_sub.add_parser("list", help="List sessions.")
    sessions_list.add_argument("--starred", action="store_true")
    sessions_list.add_argument("--archived", action="store_true")
    sessions_list.add_argument("--limit", type=int, default=None)

    sessions_events = sessions_sub.add_parser(
        "events",
        help="Print events for a session.",
    )
    sessions_events.add_argument("session_id", help="Session id (ses_...).")

    sessions_update = sessions_sub.add_parser(
        "update",
        help="Update name/starred/archived on a session.",
    )
    sessions_update.add_argument("session_id", help="Session id (ses_...).")
    sessions_update.add_argument("--name", default=None)
    sessions_update.add_argument(
        "--star",
        dest="starred",
        action="store_const",
        const=True,
        default=None,
        help="Star the session.",
    )
    sessions_update.add_argument(
        "--unstar",
        dest="starred",
        action="store_const",
        const=False,
        help="Remove the star.",
    )
    sessions_update.add_argument(
        "--archive",
        dest="archived",
        action="store_const",
        const=True,
        default=None,
    )
    sessions_update.add_argument(
        "--unarchive",
        dest="archived",
        action="store_const",
        const=False,
    )

    sessions_delete = sessions_sub.add_parser(
        "delete",
        help="Delete a session (entity record + JSONL file).",
    )
    sessions_delete.add_argument("session_id", help="Session id (ses_...).")

    # ---- reconstruct -------------------------------------------------
    sub.add_parser(
        "reconstruct",
        help="Rebuild entity records from on-disk JSONL.",
    )

    # ---- server ------------------------------------------------------
    server = sub.add_parser("server", help="Start the FastAPI server.")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8080)

    # ---- setup -------------------------------------------------------
    setup = sub.add_parser(
        "setup",
        help=(
            "First-run setup: scaffold workspace, connect provider, "
            "ship a starter agent."
        ),
    )
    setup.add_argument(
        "--non-interactive",
        dest="non_interactive",
        action="store_true",
        help=(
            "Skip all prompts. Uses KOLSCH_PROVIDER / KOLSCH_PROVIDER_KEY "
            "env vars when present to wire a provider; otherwise the "
            "starter agent ships with `provider: fake`."
        ),
    )

    # ---- connect / disconnect / providers / refresh ------------------
    connect = sub.add_parser("connect", help="Connect an LLM provider.")
    connect.add_argument("provider", help="Provider name (e.g. opencode).")

    disconnect = sub.add_parser("disconnect", help="Disconnect an LLM provider.")
    disconnect.add_argument("provider", help="Provider name.")

    sub.add_parser("providers", help="List connectors + connected providers.")

    models = sub.add_parser(
        "models",
        help=(
            "List discovered models per connected provider — useful for "
            "populating agent YAML."
        ),
    )
    models.add_argument(
        "provider",
        nargs="?",
        default=None,
        help="Provider name (default: all connected).",
    )
    models.add_argument(
        "--filter",
        dest="filter",
        default=None,
        help="Substring filter on model id (case-insensitive).",
    )
    models.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Show context_length, supports_tools, supports_streaming, "
            "pricing, and the [metadata-source] flag."
        ),
    )
    models.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Machine-readable output.",
    )

    refresh = sub.add_parser(
        "refresh",
        help="Re-run model discovery for one (or all) connected providers.",
    )
    refresh.add_argument(
        "provider", nargs="?", default=None, help="Provider name (optional)."
    )

    # kolsch permissions {list, add, remove, show}.
    from cli.permissions import add_permissions_subparser  # noqa: PLC0415

    permissions_parser = add_permissions_subparser(sub)
    if permissions_parser is not None:
        _PARSER_REGISTRY["permissions"] = permissions_parser

    # kolsch skills {list, show, which}.
    from cli.skills import add_skills_subparser  # noqa: PLC0415

    skills_parser = add_skills_subparser(sub)
    if skills_parser is not None:
        _PARSER_REGISTRY["skills"] = skills_parser

    # kolsch chat (streaming REPL).
    _add_chat_subparser(sub)

    # kolsch workflows {list, show, validate, cancel, run}.
    from cli.workflows import add_workflows_subparser  # noqa: PLC0415

    workflows_parser = add_workflows_subparser(sub)
    if workflows_parser is not None:
        _PARSER_REGISTRY["workflows"] = workflows_parser

    # kolsch nodes {list, show}.
    from cli.nodes import add_nodes_subparser  # noqa: PLC0415

    nodes_parser = add_nodes_subparser(sub)
    if nodes_parser is not None:
        _PARSER_REGISTRY["nodes"] = nodes_parser

    return parser


def _add_chat_subparser(sub: argparse._SubParsersAction[Any]) -> None:
    """Register the `kolsch chat` subparser.

    Extracted from `_build_parser` to keep that function under the
    PLR0915 statement-count ceiling.
    """
    chat = sub.add_parser(
        "chat",
        help="Open a streaming chat REPL against an agent.",
    )
    chat.add_argument(
        "agent_ref",
        nargs="?",
        default=None,
        help=(
            "Agent ref (e.g. kolsch/agents/helper). Defaults to "
            "the configured starter agent."
        ),
    )
    chat.add_argument(
        "--session",
        dest="session",
        default=None,
        help="Resume an existing session by id.",
    )
    chat.add_argument(
        "--model",
        dest="model",
        default=None,
        help="Session-scoped model override (ModelSpec input shape).",
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def _cmd_run(args: argparse.Namespace) -> int:
    from engine import ExecutionContext  # noqa: PLC0415

    runtime = await bootstrap_runtime(root=args.root)
    # Route through runtime.build_resolved_config so Layer 3
    # validators fire — see PlatformRuntime docstring.
    config = await runtime.build_resolved_config(args.agent_ref)

    # Re-targeted post-bug-fix (bug-echo-on-real-provider): warn when
    # a real-provider agent is about to run against the silent-echo
    # path because no real LLM is wired. Today's runtime constructs
    # the build_llm-backed per-spec factory only when at least one
    # provider is connected in platform.yaml's `llm_providers`. If
    # the agent's `model.provider` is real BUT the runtime fell
    # through to EchoLLMDriver, the exchange will look like a real
    # call in the event log (model.request carries the real spec)
    # while actually producing the echo. That's the bug class the
    # ticket fixed; the warning is the defensive surface so a
    # regression is loud, not silent. `provider: fake` exits the
    # check — that's the deliberate tutorial path.
    _maybe_warn_silent_echo(config, runtime)

    inputs = _parse_inputs(args.input)
    content = inputs.pop("content", None)

    # Reuse session or create a fresh one. Reusing means the thread's
    # leaf carries forward; the engine itself is stateless across
    # calls.
    if args.session:
        from schemas import SessionId  # noqa: PLC0415

        session_id = SessionId(args.session)
        session = await runtime.store.get_session(session_id)
    else:
        session = await runtime.store.create_session(
            str(config.agent_id),
            name=f"cli:{args.agent_ref}",
        )

    threads = await runtime.store.list_threads(session.session_id)
    if not threads:
        msg = f"session {session.session_id} has no threads"
        raise RuntimeError(msg)
    thread = threads[0]

    # --skill collects refs to force-load before the exchange.
    # Validate against the agent's enabled list at submit time so a
    # typo doesn't crash through to SkillForcedNotEnabled (Fatal) at
    # engine pre-flight.
    force_skills = list(getattr(args, "skill", None) or [])
    if force_skills:
        unknown = _validate_force_skill_refs(force_skills, config)
        if unknown:
            sys.stderr.write(
                f"run: --skill ref(s) not in this agent's enabled list: "
                f"{', '.join(unknown)}\n",
            )
            enabled = _enabled_skill_refs(config)
            if enabled:
                sys.stderr.write(f"enabled: {', '.join(enabled)}\n")
            else:
                sys.stderr.write(
                    "(this agent has no skills enabled in "
                    "subsystems.skills.config.enabled.)\n"
                )
            return 2

    if config.io.mode == "single_shot":
        trigger = {"kind": "invocation", "inputs": inputs}
    elif force_skills:
        trigger = {
            "kind": "user_message_with_force_skill",
            "content": content if content is not None else "",
            "force_skills": force_skills,
        }
    else:
        trigger = {
            "kind": "user_message",
            "content": content if content is not None else "",
        }

    ctx = ExecutionContext(thread_id=thread.thread_id, trigger=trigger)

    if args.stream:
        # Track whether a Fatal event landed — execute_stream doesn't
        # raise on Fatal; the failed event IS the signal. Exit 2 so
        # shell pipelines can distinguish stream success from
        # stream-with-Fatal-final-event.
        from schemas import EventType  # noqa: PLC0415

        had_fatal = False
        async for event in runtime.engine.execute_stream(config, ctx):
            sys.stdout.write(_format_event_line(event))
            sys.stdout.write("\n")
            sys.stdout.flush()
            if event.type == EventType.THREAD_EXCHANGE_FAILED:
                had_fatal = True
        return 2 if had_fatal else 0

    try:
        agent_msg = await runtime.engine.execute(config, ctx)
    except Exception as exc:  # noqa: BLE001 — surface engine-Fatal as CLI exit
        _log.exception("agent execution failed: %s", exc)
        return 2
    sys.stdout.write(json.dumps(agent_msg.data, sort_keys=True, default=str))
    sys.stdout.write("\n")
    return 0


async def _cmd_sessions_list(args: argparse.Namespace) -> int:
    """Print a compact, sorted-by-recency table of sessions.

    Four aligned columns (SESSION_ID, STARTED, LAST, NAME). The
    `--starred` / `--archived` filter flags filter the underlying
    query; they have no columns in the default output.
    """
    runtime = await bootstrap_runtime(root=args.root)
    sessions = await runtime.store.list_sessions(
        starred=(args.starred or None),
        archived=(args.archived or None),
        limit=args.limit,
    )
    if not sessions:
        return 0

    now = datetime.now(UTC)
    decorated = [
        (s, await _compute_last_event_at(runtime, s.session_id)) for s in sessions
    ]
    # Sort by last-event descending. Sessions with no events sort to
    # the bottom (None replaced by datetime.min for the key).
    decorated.sort(
        key=lambda item: item[1] or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )

    # SESSION_ID width = max(header, longest id). Session ids are
    # prefixed UUIDs (40 chars) — wider than the ticket's example
    # "ses_a1b2c3d4" suggests, but truncating them breaks the
    # round-trip contract (the displayed id must be copy-pasteable into
    # `kolsch sessions events <id>` and friends). The column expands
    # to fit; the other columns still align.
    id_width = max(
        len("SESSION_ID"),
        *(len(str(s.session_id)) for s in sessions),
    )
    sys.stdout.write(
        f"{'SESSION_ID':<{id_width}}  {'STARTED':<16}  {'LAST':<6}  NAME\n",
    )
    for session, last_at in decorated:
        started = session.created_at.astimezone().strftime("%Y-%m-%d %H:%M")
        last = _format_delta(now - last_at) if last_at is not None else "—"
        sid = f"{session.session_id!s:<{id_width}}"
        sys.stdout.write(
            f"{sid}  {started:<16}  {last:<6}  {session.name}\n",
        )
    return 0


_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86_400


def _format_delta(delta: timedelta) -> str:
    """Render a non-negative timedelta as a compact ago-style string.

    Compact units only — no "just now" / "1y" prose: the column is
    fixed-width and grep-friendly.
    """
    seconds = int(delta.total_seconds())
    if seconds < _SECONDS_PER_MINUTE:
        return f"{seconds}s"
    if seconds < _SECONDS_PER_HOUR:
        return f"{seconds // _SECONDS_PER_MINUTE}m"
    if seconds < _SECONDS_PER_DAY:
        return f"{seconds // _SECONDS_PER_HOUR}h"
    return f"{seconds // _SECONDS_PER_DAY}d"


async def _compute_last_event_at(
    runtime: PlatformRuntime, session_id: object
) -> datetime | None:
    """Return the max `created_at` across each thread's last event.

    Walks the session's threads via `runtime.store.list_threads` and
    reads the last event per thread via
    [`read_last_event_for_thread`][store.jsonl.read_last_event_for_thread].
    Returns `None` when the session has no events yet (rare — created
    but never run).

    One JSONL tail-scan per thread per session. Fast for typical
    workspaces (<100 sessions); pathological cases (10k+ sessions)
    are flagged as a deferred optimization.
    """
    from schemas import SessionId  # noqa: PLC0415
    from store.jsonl import (  # noqa: PLC0415
        jsonl_path,
        read_last_event_for_thread,
    )

    typed_session_id = SessionId(str(session_id))
    threads = await runtime.store.list_threads(typed_session_id)
    path = jsonl_path(runtime.root, typed_session_id)
    latest: datetime | None = None
    for thread in threads:
        event = read_last_event_for_thread(path, thread.thread_id)
        if event is None:
            continue
        if latest is None or event.created_at > latest:
            latest = event.created_at
    return latest


async def _cmd_sessions_events(args: argparse.Namespace) -> int:
    from schemas import SessionId  # noqa: PLC0415

    runtime = await bootstrap_runtime(root=args.root)
    session_id = SessionId(args.session_id)
    threads = await runtime.store.list_threads(session_id)
    for thread in threads:
        async for event in runtime.store.read_events(thread.thread_id):
            sys.stdout.write(_format_event_line(event))
            sys.stdout.write("\n")
    return 0


async def _cmd_sessions_update(args: argparse.Namespace) -> int:
    from schemas import SessionId  # noqa: PLC0415

    runtime = await bootstrap_runtime(root=args.root)
    sid = SessionId(args.session_id)
    updated = await runtime.store.update_session(
        sid,
        name=args.name,
        starred=args.starred,
        archived=args.archived,
    )
    sys.stdout.write(
        f"{updated.session_id}  starred={updated.starred}  "
        f"archived={updated.archived}  name={updated.name}\n",
    )
    return 0


async def _cmd_sessions_delete(args: argparse.Namespace) -> int:
    from schemas import SessionId  # noqa: PLC0415

    runtime = await bootstrap_runtime(root=args.root)
    sid = SessionId(args.session_id)
    await runtime.store.delete_session(sid)
    sys.stdout.write(f"deleted {sid}\n")
    return 0


async def _cmd_sessions(args: argparse.Namespace) -> int:
    """Dispatch the `sessions` op.

    When the user typed `kolsch sessions` with no operation,
    `args.sessions_command` is `None` — print the subparser's help
    to stderr and exit 1 instead of falling through to
    `_cmd_sessions_list` (which would crash on missing
    `args.starred` / `args.archived` / `args.limit`).
    """
    if args.sessions_command is None:
        return _print_subcommand_help_and_exit("sessions")
    if args.sessions_command == "list":
        return await _cmd_sessions_list(args)
    if args.sessions_command == "events":
        return await _cmd_sessions_events(args)
    if args.sessions_command == "update":
        return await _cmd_sessions_update(args)
    if args.sessions_command == "delete":
        return await _cmd_sessions_delete(args)
    return 1


def _print_subcommand_help_and_exit(command: str) -> int:
    """Print a multi-subcommand subparser's help to stderr; return 1.

    Used by `_cmd_sessions` and `_cmd_permissions` when the user
    invokes the parent subcommand without specifying an op. Shows
    available operations; no Python traceback.
    """
    subparser = _PARSER_REGISTRY.get(command)
    if subparser is not None:
        subparser.print_help(file=sys.stderr)
    else:
        sys.stderr.write(
            f"{command}: missing operation. Run `kolsch {command} --help`.\n"
        )
    return 1


async def _cmd_reconstruct(args: argparse.Namespace) -> int:
    runtime = await bootstrap_runtime(root=args.root)
    result = await runtime.store.reconstruct_from_filesystem()
    sys.stdout.write(json.dumps(result.model_dump(mode="json")) + "\n")
    return 0


async def _cmd_server(args: argparse.Namespace) -> int:
    import uvicorn  # noqa: PLC0415

    from api import build_app  # noqa: PLC0415

    runtime = await bootstrap_runtime(root=args.root)
    app = build_app(runtime)
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()
    return 0


async def _cmd_connect(args: argparse.Namespace) -> int:
    # Eager-trigger provider registration so the registry sees both
    # reference connectors before connect/disconnect/providers/refresh
    # run.
    import llm.providers  # noqa: F401, PLC0415
    from cli.llm_commands import cmd_connect, make_default_io  # noqa: PLC0415

    return await cmd_connect(args, make_default_io(root=args.root))


async def _cmd_disconnect(args: argparse.Namespace) -> int:
    import llm.providers  # noqa: F401, PLC0415
    from cli.llm_commands import cmd_disconnect, make_default_io  # noqa: PLC0415

    return await cmd_disconnect(args, make_default_io(root=args.root))


async def _cmd_providers(args: argparse.Namespace) -> int:
    import llm.providers  # noqa: F401, PLC0415
    from cli.llm_commands import cmd_providers, make_default_io  # noqa: PLC0415

    return await cmd_providers(args, make_default_io(root=args.root))


async def _cmd_refresh(args: argparse.Namespace) -> int:
    import llm.providers  # noqa: F401, PLC0415
    from cli.llm_commands import cmd_refresh, make_default_io  # noqa: PLC0415

    return await cmd_refresh(args, make_default_io(root=args.root))


async def _cmd_models(args: argparse.Namespace) -> int:
    import llm.providers  # noqa: F401, PLC0415
    from cli.llm_commands import cmd_models, make_default_io  # noqa: PLC0415

    return await cmd_models(args, make_default_io(root=args.root))


async def _cmd_permissions(args: argparse.Namespace) -> int:
    """Dispatch the `permissions` op.

    Mirrors `_cmd_sessions`: when no op is supplied, print the
    subparser's help to stderr and exit 1 rather than the awkward
    `"unknown permissions op None"` the inner handler emits.
    """
    if args.permissions_command is None:
        return _print_subcommand_help_and_exit("permissions")
    from cli.permissions import cmd_permissions  # noqa: PLC0415

    return await cmd_permissions(args)


async def _cmd_skills(args: argparse.Namespace) -> int:
    """Dispatch the `skills` op.

    Same help-and-exit pattern as `_cmd_sessions` /
    `_cmd_permissions` for the missing-op case.
    """
    if args.skills_command is None:
        return _print_subcommand_help_and_exit("skills")
    from cli.skills import cmd_skills  # noqa: PLC0415

    return await cmd_skills(args)


async def _cmd_chat(args: argparse.Namespace) -> int:
    from cli.chat import cmd_chat  # noqa: PLC0415

    return await cmd_chat(args)


async def _cmd_setup(args: argparse.Namespace) -> int:
    # Setup orchestrates `connect` + `run` against the workspace it
    # creates; import lazily so importing `cli.main` doesn't pull the
    # six-step setup module + the runtime composition root into the
    # `kolsch --help` path.
    from cli._setup import make_default_setup_io, run_setup  # noqa: PLC0415

    return await run_setup(args, make_default_setup_io(root=args.root))


async def _cmd_workflows(args: argparse.Namespace) -> int:
    from cli.workflows import cmd_workflows  # noqa: PLC0415

    # Resolve sys.stdout/sys.stderr at call time so capsys redirects work.
    return await cmd_workflows(args, out=sys.stdout, err=sys.stderr)


async def _cmd_nodes(args: argparse.Namespace) -> int:
    from cli.nodes import cmd_nodes  # noqa: PLC0415

    return await cmd_nodes(args, out=sys.stdout, err=sys.stderr)


_DISPATCH = {
    "run": _cmd_run,
    "sessions": _cmd_sessions,
    "reconstruct": _cmd_reconstruct,
    "server": _cmd_server,
    "connect": _cmd_connect,
    "disconnect": _cmd_disconnect,
    "providers": _cmd_providers,
    "refresh": _cmd_refresh,
    "models": _cmd_models,
    "setup": _cmd_setup,
    "permissions": _cmd_permissions,
    "skills": _cmd_skills,
    "chat": _cmd_chat,
    "workflows": _cmd_workflows,
    "nodes": _cmd_nodes,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _maybe_warn_silent_echo(
    config: ResolvedAgentConfig, runtime: PlatformRuntime
) -> None:
    """Warn on stderr when a real-provider agent would silently echo.

    Fires when:

    - `config.model.provider != "fake"` (the agent intends a real
      call), AND
    - The runtime did not wire `llm_for_model` (i.e. engine has only
      the `EchoLLMDriver` fallback). Normally this only happens when
      no providers are connected; the warning catches any future
      regression that silently routes a real-provider agent through
      the echo path.

    Agents that explicitly use `provider: fake` are silent — that's
    the tutorial path; the user knows what they signed up for.
    """
    if config.model.provider == "fake":
        return
    if runtime.engine._llm_for_model is not None:
        return
    sys.stderr.write(
        f"⚠ this agent specifies provider {config.model.provider!r}, but "
        f"no real-LLM wiring is active — the exchange will silently echo "
        f"via the built-in fake LLM. Run `kolsch connect "
        f"{config.model.provider}` to wire a real provider.\n"
    )


def _enabled_skill_refs(config: object) -> list[str]:
    """Return the agent's enabled skill refs in declaration order.

    Mirrors `cli._slash._enabled_skill_refs` (kept duplicated so
    `main.py` does not pull the chat module in for a one-line read of
    its own typed config). Returns `[]` when the subsystem is absent.
    """
    subsystems = getattr(config, "subsystems", None) or {}
    sub = subsystems.get("skills") if isinstance(subsystems, dict) else None
    if sub is None:
        return []
    body = getattr(sub, "config", None) or {}
    out: list[str] = []
    for entry in body.get("enabled") or []:
        if isinstance(entry, dict):
            ref = entry.get("ref")
            if isinstance(ref, str) and ref:
                out.append(ref)
    return out


def _validate_force_skill_refs(refs: list[str], config: object) -> list[str]:
    """Return the subset of `refs` not in the agent's enabled skills.

    Surfaces typos at submit time instead of failing with
    `errors.SkillForcedNotEnabled` (Fatal) at engine pre-flight,
    which would also discard the just-submitted user message.
    """
    enabled = set(_enabled_skill_refs(config))
    return [r for r in refs if r not in enabled]


def _parse_inputs(raw: list[str]) -> dict[str, str]:
    """Parse `--input KEY=VALUE` pairs into a dict.

    Multiple `--input` flags accumulate. Values are strings; the
    engine's IO type system will coerce per the agent's `IOSpec`.
    """
    out: dict[str, str] = {}
    for item in raw:
        if "=" not in item:
            msg = f"--input must be of the form KEY=VALUE; got {item!r}"
            raise SystemExit(msg)
        key, value = item.split("=", 1)
        out[key] = value
    return out


def _format_event_line(event: object) -> str:
    """Render an event as a single JSON-line for terminal output."""
    payload = event.model_dump(mode="json") if hasattr(event, "model_dump") else event
    return json.dumps(payload, sort_keys=True, default=str)
