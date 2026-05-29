"""``pi-platform`` CLI dispatcher.

Argparse-based — no external CLI dep. Each subcommand has a small
async handler in this module; the top-level :func:`main` is the
entry point bound to the ``pi-platform`` console script.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from platform_runtime import bootstrap_runtime

if TYPE_CHECKING:
    from collections.abc import Sequence


_log = logging.getLogger("cli")


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point. Returns the process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    logging.basicConfig(
        level=logging.WARNING if not args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    handler = _DISPATCH[args.command]
    return asyncio.run(handler(args))


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-platform",
        description="Agent Platform CLI (v1 default tier).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Platform root (defaults to CWD).",
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
        help="Full agent ref (e.g. acme/agents/researcher).",
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

    # ---- sessions ----------------------------------------------------
    sessions = sub.add_parser("sessions", help="Session operations.")
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

    # ---- reconstruct -------------------------------------------------
    sub.add_parser(
        "reconstruct",
        help="Rebuild entity records from on-disk JSONL.",
    )

    # ---- server ------------------------------------------------------
    server = sub.add_parser("server", help="Start the FastAPI server.")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8080)

    return parser


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def _cmd_run(args: argparse.Namespace) -> int:
    from engine import ExecutionContext  # noqa: PLC0415

    runtime = await bootstrap_runtime(root=args.root)
    config = await runtime.asset_management.build_resolved_config(args.agent_ref)

    inputs = _parse_inputs(args.input)
    content = inputs.pop("content", None)

    # Reuse session or create a fresh one. Reusing means the thread's
    # leaf carries forward (engine I8 — statelessness across calls).
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

    if config.io.mode == "single_shot":
        trigger = {"kind": "invocation", "inputs": inputs}
    else:
        trigger = {
            "kind": "user_message",
            "content": content if content is not None else "",
        }

    ctx = ExecutionContext(thread_id=thread.thread_id, trigger=trigger)

    if args.stream:
        async for event in runtime.engine.execute_stream(config, ctx):
            sys.stdout.write(_format_event_line(event))
            sys.stdout.write("\n")
            sys.stdout.flush()
        return 0

    try:
        agent_msg = await runtime.engine.execute(config, ctx)
    except Exception as exc:  # noqa: BLE001 — surface engine-Fatal as CLI exit
        _log.exception("agent execution failed: %s", exc)
        return 2
    sys.stdout.write(json.dumps(agent_msg.data, sort_keys=True, default=str))
    sys.stdout.write("\n")
    return 0


async def _cmd_sessions_list(args: argparse.Namespace) -> int:
    runtime = await bootstrap_runtime(root=args.root)
    sessions = await runtime.store.list_sessions(
        starred=(args.starred or None),
        archived=(args.archived or None),
        limit=args.limit,
    )
    for s in sessions:
        sys.stdout.write(
            f"{s.session_id}  agent={s.agent_id}  name={s.name}  "
            f"starred={s.starred}  archived={s.archived}\n",
        )
    return 0


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


async def _cmd_sessions(args: argparse.Namespace) -> int:
    if args.sessions_command == "list" or args.sessions_command is None:
        return await _cmd_sessions_list(args)
    if args.sessions_command == "events":
        return await _cmd_sessions_events(args)
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


_DISPATCH = {
    "run": _cmd_run,
    "sessions": _cmd_sessions,
    "reconstruct": _cmd_reconstruct,
    "server": _cmd_server,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_inputs(raw: list[str]) -> dict[str, str]:
    """Parse ``--input KEY=VALUE`` pairs into a dict.

    Multiple ``--input`` flags accumulate. Values are strings; the
    engine's IO type system will coerce per the agent's ``IOSpec``.
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
