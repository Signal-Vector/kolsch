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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cli._logging import configure_cli_logging
from errors import AssetsNotFound, ConfigInvalid, PlatformError
from platform_runtime import (
    bootstrap_runtime,
    resolve_workspace,
    workspace_is_initialized,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from platform_runtime import PlatformRuntime
    from schemas import Event, ResolvedAgentConfig, Session, SessionId


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


def _format_resolve_config_error(exc: PlatformError, *, agent_ref: str) -> str:
    """Render a `build_resolved_config` failure as readable stderr text.

    `workspace_is_initialized` (checked in `main()`) only proves
    `platform.yaml` exists — not that it's complete. A workspace left
    mid-`kolsch setup` (interrupted before Step D writes
    `defaults.model`) passes that gate and then fails here instead,
    inside `runtime.build_resolved_config`. Without this formatter
    that failure was an uncaught `PlatformError` — a raw traceback
    with no indication of what to do about it.

    The hint line is deliberately keyed off exception type:
    `ConfigInvalid` (the motivating case — an incomplete/malformed
    config) genuinely IS fixed by re-running `kolsch setup`, but a
    typo'd or renamed ref (`AssetsNotFound`) is not — pointing that
    user at `kolsch setup` would be actively misleading.
    """
    lines = [f"error: could not resolve agent {agent_ref!r}: {exc}"]
    if isinstance(exc, ConfigInvalid):
        for finding in exc.detail.get("validation_errors", []):
            path = finding.get("path", "?")
            message = finding.get("message", "")
            lines.append(f"  - {path}: {message}")
        lines.append(
            "Run `kolsch setup` to finish configuring this workspace "
            "(or `kolsch doctor` to diagnose it)."
        )
    elif isinstance(exc, AssetsNotFound):
        lines.append(
            "Check the ref — agents live under "
            "<workspace>/bundles/<bundle>/agents/<name>.yaml."
        )
    else:
        lines.append("Run `kolsch doctor` to diagnose the workspace.")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the console-script entry point and return the process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    # Resolve the workspace once, intercept the missing-workspace case
    # before any subsystem boots so the user gets a friendly message
    # instead of a stack trace. `setup` is the one command exempt from
    # the initialization gate; it's what populates the workspace.
    # `doctor` is also exempt — its job is to diagnose the workspace
    # (including "no workspace at this path"), so a stack trace would
    # be worse than a report saying "nothing to inspect here".
    resolved_root = resolve_workspace(args.root)
    initialized = workspace_is_initialized(resolved_root)
    # bug-148 — stderr shows kolsch's own loggers (all with `-v`); an
    # initialized workspace also gets `logs/kolsch.log` with every
    # record. Nothing logs before this point.
    configure_cli_logging(
        verbose=args.verbose, workspace_root=resolved_root if initialized else None
    )
    # `mcp bridge` (spec-120) is also exempt: it is a pure socket
    # proxy an EXTERNAL harness spawns — all state lives in the
    # running kolsch process behind KOLSCH_MCP_SOCKET, so requiring a
    # workspace here would break spawning from any cwd/env.
    workspace_exempt = args.command in {"setup", "doctor"} or (
        args.command == "mcp" and getattr(args, "mcp_command", None) == "bridge"
    )
    if not workspace_exempt and not initialized:
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
    return asyncio.run(_run_handler_and_drain_hooks(handler, args))


async def _run_handler_and_drain_hooks(
    handler: Callable[[argparse.Namespace], Awaitable[int]],
    args: argparse.Namespace,
) -> int:
    """Run `handler(args)`, then give in-flight hook tasks a grace window.

    **spec-131 Step 6 finding, not anticipated by the original spec:**
    every CLI subcommand is a short-lived process — `asyncio.run()`
    tears down the event loop the moment its coroutine returns. A
    configured `hooks:` rule fires its command as a fire-and-forget
    background task (`HookDispatcher.handle`, Step 4 — deliberately
    never awaited, so a slow hook never delays the engine's own event
    stream). If that task is still suspended inside
    `proc.communicate()` when the handler coroutine returns,
    cancelling it during `asyncio.run()`'s shutdown sequence does not
    reliably deliver on this platform (empirically reproduced:
    spawning a subprocess inside an unawaited `asyncio.create_task`
    then returning from `main()` immediately hangs the process
    indefinitely on macOS + CPython 3.13's child-watcher/event-loop-
    shutdown interaction — 100% reproducible, independent of any
    kolsch-specific code) — wedging the WHOLE CLI process forever
    instead of exiting with the handler's return code.

    This wraps every subcommand at the single dispatch choke point
    (`main`'s `asyncio.run(...)` call) rather than fixing each command
    handler individually, mirroring the injected-single-choke-point
    philosophy the rest of spec-131 uses (Step 5's engine-level
    observer seam). `kolsch chat` is unaffected in practice — bug-116's
    `force_process_exit` hard-exits via `os._exit()` before this
    coroutine would ever return normally, and a long-lived chat session
    gives any hook task ample real time to finish well before that
    hard-exit fires anyway.

    See `platform_runtime.hooks.drain_background_hook_tasks`'s
    docstring for the full root-cause writeup and why the fix belongs
    here (waiting it out) rather than inside `HookDispatcher`/
    `run_hook_command` (cancellation delivery during interpreter/loop
    shutdown is outside kolsch's control).
    """
    from platform_runtime.hooks import drain_background_hook_tasks  # noqa: PLC0415

    try:
        return await handler(args)
    finally:
        await drain_background_hook_tasks()


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
        help=(
            "Enable INFO-level logging on stderr, including third-party "
            "libraries and failure tracebacks."
        ),
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # ---- run ---------------------------------------------------------
    run = sub.add_parser("run", help="Execute one agent exchange.")
    run.add_argument(
        "agent_ref",
        help="Full agent ref (e.g. kolsch/agents/helper).",
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
    run.add_argument(
        "--model",
        type=str,
        default=None,
        metavar="<provider>/<model>",
        help=(
            "Override the agent's model for THIS run only (no file edit). "
            "Accepts the same forms as a YAML model: a compact "
            "'provider/model' string or a platform.yaml model_aliases alias. "
            "The agent's fallback chain is cleared for the override; the "
            "model's provider must be connected."
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
            "starter agent ships with `provider: mock`."
        ),
    )
    # qol-093 — bundle-drift repair. When --sync-bundle is passed,
    # setup skips the install flow and runs the sync pass instead
    # (rewrites kolsch-owned drift per the qol-089 migrator +
    # baseline; reports user-owned drift). --dry-run touches nothing.
    # --yes / --no drive per-file default answers in non-interactive
    # runs. Both keep locally-modified files. --yes copies new files and
    # deletes a retired-at-source file only when the baseline proves
    # kolsch shipped it unedited (bug-146); --no copies and deletes
    # nothing. Any kept item triggers the partial-success exit (1).
    setup.add_argument(
        "--sync-bundle",
        dest="sync_bundle",
        action="store_true",
        help=(
            "Repair `bundles/kolsch/**` drift against the packaged "
            "source (qol-093). Rewrites kolsch-owned drift; reports "
            "user-owned drift; never writes user bundles."
        ),
    )
    setup.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="With --sync-bundle: print planned changes; write nothing.",
    )
    setup.add_argument(
        "--yes",
        dest="yes",
        action="store_true",
        help=(
            "With --sync-bundle: non-interactive. Copies new shipped files; "
            "keeps locally-modified files; deletes a file no longer shipped "
            "only when the baseline shows kolsch shipped it unedited (else "
            "keeps it). Exits 1 (partial) if anything was kept."
        ),
    )
    setup.add_argument(
        "--no",
        dest="no",
        action="store_true",
        help=(
            "With --sync-bundle: non-interactive. Copies and deletes "
            "nothing: keeps modified and no-longer-shipped files and skips "
            "new ones (retired-tool ref rewrites still apply). Exits 1 "
            "(partial) if anything was kept."
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

    # kolsch doctor — read-only bundle-drift report (qol-089).
    from cli._doctor import add_doctor_subparser  # noqa: PLC0415

    add_doctor_subparser(sub)

    # kolsch mcp {serve} — MCP operator server (spec-105). Nested
    # subparser so future subactions (`inspect`, `probe`, ...) slot
    # in without a schema break; today only `serve` exists.
    mcp = sub.add_parser(
        "mcp",
        help="MCP operator server (spec-105) — read-only stdio bridge.",
    )
    _PARSER_REGISTRY["mcp"] = mcp
    mcp_sub = mcp.add_subparsers(dest="mcp_command", metavar="<op>")
    mcp_sub.add_parser(
        "serve",
        help=(
            "Serve the MCP operator server on stdio. Pairs with "
            "`claude -p --mcp-config` under a Pro/Max subscription."
        ),
    )
    # kolsch mcp bridge — the tool-bridge stdio proxy (spec-120). A
    # stateless subprocess an external MCP-client harness spawns; it
    # forwards tools/list + tools/call over KOLSCH_MCP_SOCKET into a
    # running kolsch exchange. Distinct from `serve` (the read-only
    # operator server).
    mcp_sub.add_parser(
        "bridge",
        help=(
            "Serve the MCP tool bridge on stdio (spec-120). Spawned by "
            "an external agent harness; requires KOLSCH_MCP_SOCKET and "
            "KOLSCH_MCP_TOKEN from a running kolsch exchange."
        ),
    )

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
    chat.add_argument(
        "--full-history",
        dest="full_history",
        action="store_true",
        default=False,
        help=(
            "Replay the whole session history on --session resume "
            "instead of the default tail-20 turns."
        ),
    )
    chat.add_argument(
        "--resume",
        dest="resume",
        action="store_true",
        default=False,
        help=(
            "Show an interactive arrow-key picker of recent sessions "
            "and resume the chosen one. Conflicts with --session."
        ),
    )
    chat.add_argument(
        "--auto-approve",
        dest="auto_approve",
        action="store_true",
        default=False,
        help=(
            "Auto-approve EVERY tool authorization prompt (allow-once, no "
            "persisted rule) for the whole session — bypasses all approval "
            "gating. Cards still render and flip to '✓ Auto-approved "
            "(once)'. Use only in trusted, low-risk contexts."
        ),
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def apply_run_model_override(
    config: ResolvedAgentConfig,
    model_ref: str,
    runtime: PlatformRuntime,
) -> ResolvedAgentConfig | None:
    """Replace `config`'s model with the `--model` override, or return `None`.

    Returns `None` (after printing a stderr error) when `model_ref` is
    malformed, an unknown alias, or names an unconnected provider. The
    override accepts the SAME grammar as a YAML `model:` string — a compact
    `provider/model` or a `platform.yaml` `model_aliases` alias — via the
    shared `assets.assembler._normalize_model_value`. It clears the agent's
    `fallback_models` (an explicit single-model CLI choice), and its provider
    must be connected (in `llm_providers`) or be `mock`, else it fails fast
    so the run doesn't silently fall through to the echo path.
    """
    from assets.assembler import _normalize_model_value  # noqa: PLC0415
    from schemas import ModelSpec  # noqa: PLC0415

    try:
        raw_aliases = runtime.asset_management.get_platform_config("model_aliases")
    except KeyError:
        raw_aliases = {}  # no model_aliases section — compact strings still work
    aliases = {k: v for k, v in raw_aliases.items() if isinstance(v, str)}
    try:
        normalized = _normalize_model_value(model_ref, aliases)
        if not isinstance(normalized, dict):
            msg = f"unexpected normalization shape: {type(normalized).__name__}"
            raise TypeError(msg)  # noqa: TRY301
        spec = ModelSpec(**normalized)
    except Exception as exc:  # noqa: BLE001 — surface every validation flavour
        sys.stderr.write(f"run: --model {model_ref!r} is invalid: {exc}\n")
        return None
    if spec.provider != "mock" and spec.provider not in runtime.llm_providers_config:
        connected = ", ".join(sorted(runtime.llm_providers_config)) or "(none)"
        sys.stderr.write(
            f"run: --model provider {spec.provider!r} is not connected. "
            f"Run `kolsch connect {spec.provider}` first (connected: {connected}).\n"
        )
        return None
    return config.model_copy(update={"model": spec, "fallback_models": []})


@dataclass(frozen=True)
class _RunPreflightFailure:
    """Why a run stopped before the engine was called — no exchange ran.

    - `message` — one line, no `error:` prefix (e.g. `could not resolve
      agent 'x': …`). The core has already printed the full diagnostic
      to stderr; this is the structured copy for callers (setup's
      banner) that must not claim an exchange failed.
    - `code` — the `PlatformError` code when the cause was one (config
      resolve, `--session` store lookup); `None` otherwise.
    """

    message: str
    code: str | None = None


@dataclass(frozen=True)
class _RunOutcome:
    """What one `kolsch run`-shaped agent invocation produced.

    Returned by [`_run_agent_once`][cli.main._run_agent_once], the shared
    run-once seam that both `kolsch run` (via `_cmd_run`) and `kolsch
    setup`'s Step E test run call. Callers render the failure however
    their surface needs: the seam logs nothing and never prints the
    exception (see `_run_agent_once` for what it does print).

    - `rc` — the exit code `kolsch run` returns: `0` success, `2` any
      failure (pre-flight, engine Fatal, unexpected exception, or a
      `--stream` run whose last event was Fatal).
    - `session_id` — the session the run created or reused; `None` when
      the run returned before a session was resolved (config resolve,
      `--model`, or a malformed / unknown `--session`).
    - `agent_message` — the final agent-message event on a successful
      non-stream run; `None` otherwise.
    - `failed_event` — the durable `thread.exchange_failed` event when
      the exchange ended Fatal, on both the `execute` path (from
      `AgentExecutionError.failed_event`) and the `--stream` path. Its
      `data["error"]` is the durable error envelope.
    - `exc` — the exception caught around `engine.execute`:
      `AgentExecutionError` (paired with `failed_event`) or any other
      `Exception` (alone; an unexpected bug). Always `None` on the
      `--stream` path, which never raises on Fatal.
    - `preflight` — set when the run stopped BEFORE `engine.execute` /
      `execute_stream` was called (config resolve, `--model`,
      `--session`, a session with no threads, `--skill`); then no
      exchange ran, and `failed_event` and `exc` are `None`. See
      [`_RunPreflightFailure`][cli.main._RunPreflightFailure].
    """

    rc: int
    session_id: SessionId | None = None
    agent_message: Event | None = None
    failed_event: Event | None = None
    exc: Exception | None = None
    preflight: _RunPreflightFailure | None = None


async def _cmd_run(args: argparse.Namespace) -> int:
    """`kolsch run` — a thin rendering wrapper over `_run_agent_once`.

    Failure output is concise and traceback-free by default (bug-148):

    - Engine Fatal (`AgentExecutionError`) — the failure block from
      [`format_exchange_failure`][cli._run_failure.format_exchange_failure]
      (code, message, one-line cause, session id, `kolsch sessions
      events <id>` hint) on stderr, exit 2.
    - Any other exception from `engine.execute` (a kolsch bug) — the
      one-line internal-error message from
      [`format_internal_failure`][cli._run_failure.format_internal_failure],
      exit 2.
    - Pre-flight failures (config resolve, `--model`, `--session`, no
      thread, `--skill`) — the `error: …` line the core already
      printed, exit 2.

    In both exception cases the traceback is logged on the `cli` logger
    at INFO, so it reaches stderr only under `kolsch -v` (and the
    workspace log file). Nothing is written to stdout on failure; on
    success the agent message JSON is printed, unchanged. `--stream`
    event lines are written by the core, unchanged.
    """
    from cli._run_failure import (  # noqa: PLC0415
        format_exchange_failure,
        format_internal_failure,
    )

    outcome = await _run_agent_once(args)
    if outcome.exc is not None:
        if outcome.failed_event is not None:
            sys.stderr.write(
                format_exchange_failure(outcome.failed_event, agent_ref=args.agent_ref)
            )
            _log.info(
                "agent execution failed (event %s)",
                outcome.failed_event.event_id,
                exc_info=outcome.exc,
            )
        else:
            # The kolsch-bug path: not an ordinary engine-Fatal failure.
            sys.stderr.write(
                format_internal_failure(
                    outcome.exc,
                    agent_ref=args.agent_ref,
                    workspace_root=resolve_workspace(args.root),
                )
            )
            _log.info("agent execution raised unexpectedly", exc_info=outcome.exc)
    if outcome.agent_message is not None:
        _write_agent_message(outcome.agent_message)
    return outcome.rc


def _write_agent_message(agent_msg: Event) -> None:
    """Print a successful run's agent message as one JSON line on stdout."""
    sys.stdout.write(json.dumps(agent_msg.data, sort_keys=True, default=str))
    sys.stdout.write("\n")


async def _run_agent_once(args: argparse.Namespace) -> _RunOutcome:
    """Run one agent exchange from `kolsch run`-shaped `args`.

    The run-once seam shared by `kolsch run` and `kolsch setup`'s Step E:
    resolves the agent config, applies `--model`, creates (or reuses,
    with `--session`) the session, validates `--skill`, then calls the
    engine once. Returns a [`_RunOutcome`][cli.main._RunOutcome] instead
    of an exit code so callers get the session id and the durable
    `thread.exchange_failed` event, not just `rc`.

    Output contract: pre-flight diagnostics (config resolve, `--model`,
    `--session`, a session with no threads, `--skill`) go to stderr
    here and are also returned as `outcome.preflight`; `--stream` event
    lines go to stdout here, exactly as `kolsch run` prints them. The
    success-path agent message JSON and the failure rendering / logging
    of a caught exception are left to the caller.

    Exceptions: every exception raised by `engine.execute` is caught and
    reported on the outcome (never logged here). Outside that call, only
    the expected pre-flight failures are caught — a `PlatformError` from
    config resolve or the `--session` lookup, and the `ValueError` of a
    malformed `--session` id; anything else (e.g. a runtime bootstrap
    failure) propagates unchanged.
    """
    from engine import AgentExecutionError, ExecutionContext  # noqa: PLC0415

    runtime = await bootstrap_runtime(root=args.root)
    # Route through runtime.build_resolved_config so Layer 3
    # validators fire — see PlatformRuntime docstring.
    try:
        config = await runtime.build_resolved_config(args.agent_ref)
    except PlatformError as exc:
        sys.stderr.write(_format_resolve_config_error(exc, agent_ref=args.agent_ref))
        return _RunOutcome(
            rc=2,
            preflight=_RunPreflightFailure(
                message=f"could not resolve agent {args.agent_ref!r}: {exc}",
                code=exc.code,
            ),
        )

    # qol-122 — `--model` overrides the resolved model for THIS run only,
    # applied before the silent-echo check so the warning reflects it.
    if getattr(args, "model", None):
        overridden = apply_run_model_override(config, args.model, runtime)
        if overridden is None:
            # The specific error is already printed to stderr.
            return _RunOutcome(
                rc=2,
                preflight=_RunPreflightFailure(
                    message=f"--model {args.model!r} could not be applied"
                ),
            )
        config = overridden

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
    # regression is loud, not silent. `provider: mock` exits the
    # check — that's the deliberate tutorial path.
    _maybe_warn_silent_echo(config, runtime)

    inputs = _parse_inputs(args.input)
    content = inputs.pop("content", None)

    # Reuse session or create a fresh one. Reusing means the thread's
    # leaf carries forward; the engine itself is stateless across
    # calls.
    if args.session:
        resumed = await _resolve_run_session(runtime, args.session)
        if isinstance(resumed, _RunPreflightFailure):
            return _RunOutcome(rc=2, preflight=resumed)
        session = resumed
    else:
        session = await runtime.store.create_session(
            str(config.agent_id),
            name=f"cli:{args.agent_ref}",
        )
    session_id = session.session_id

    threads = await runtime.store.list_threads(session_id)
    if not threads:
        message = f"session {session_id} has no threads"
        sys.stderr.write(
            f"error: {message}\nRun without --session to start a new session.\n"
        )
        return _RunOutcome(
            rc=2,
            session_id=session_id,
            preflight=_RunPreflightFailure(message=message),
        )
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
            return _RunOutcome(
                rc=2,
                session_id=session_id,
                preflight=_RunPreflightFailure(
                    message=(
                        "--skill ref(s) not in this agent's enabled list: "
                        + ", ".join(unknown)
                    )
                ),
            )

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

        failed_event: Event | None = None
        async for event in runtime.engine.execute_stream(config, ctx):
            sys.stdout.write(_format_event_line(event))
            sys.stdout.write("\n")
            sys.stdout.flush()
            if event.type == EventType.THREAD_EXCHANGE_FAILED:
                failed_event = event
        return _RunOutcome(
            rc=2 if failed_event is not None else 0,
            session_id=session_id,
            failed_event=failed_event,
        )

    try:
        agent_msg = await runtime.engine.execute(config, ctx)
    except AgentExecutionError as exc:
        return _RunOutcome(
            rc=2, session_id=session_id, failed_event=exc.failed_event, exc=exc
        )
    # Not an ordinary failure (those arrive as AgentExecutionError):
    # the kolsch-bug path, reported on the outcome for the caller.
    except Exception as exc:  # noqa: BLE001 — unexpected engine exception
        return _RunOutcome(rc=2, session_id=session_id, exc=exc)
    return _RunOutcome(rc=0, session_id=session_id, agent_message=agent_msg)


async def _resolve_run_session(
    runtime: PlatformRuntime, raw_session_id: str
) -> Session | _RunPreflightFailure:
    """Look up the `--session` a run resumes, or describe why it can't.

    A malformed id (`SessionId`'s `ValueError`) and a store failure
    (`PlatformError`, e.g. `SessionNotFound`) each print one `error: …`
    line (plus a hint) to stderr and return a
    [`_RunPreflightFailure`][cli.main._RunPreflightFailure] instead of
    raising, so `kolsch run` never shows a traceback for a bad flag.
    """
    from errors import SessionNotFound  # noqa: PLC0415
    from schemas import SessionId  # noqa: PLC0415

    hint = "Run `kolsch sessions list` to see existing sessions.\n"
    try:
        session_id = SessionId(raw_session_id)
    except ValueError as exc:
        message = f"invalid --session {raw_session_id!r}: {exc}"
        sys.stderr.write(f"error: {message}\n{hint}")
        return _RunPreflightFailure(message=message)
    try:
        return await runtime.store.get_session(session_id)
    except PlatformError as exc:
        message = f"{exc.code}: {exc}"
        sys.stderr.write(f"error: {message}\n")
        if isinstance(exc, SessionNotFound):
            sys.stderr.write(hint)
        return _RunPreflightFailure(message=message, code=exc.code)


async def _recent_sessions_decorated(
    runtime: PlatformRuntime,
    *,
    starred: bool | None = None,
    archived: bool | None = None,
    limit: int | None = None,
) -> list[tuple[Any, datetime | None]]:
    """Return recency-sorted ``(Session, last_activity_at)`` tuples.

    Shared by :func:`_cmd_sessions_list` (the ``sessions list`` command)
    and :func:`_cmd_chat`'s ``--resume`` picker so the two never
    diverge on ordering. Sessions with no events sort to the bottom
    (their ``last_activity_at`` is ``None`` and the sort key falls
    back to ``datetime.min``).
    """
    sessions = await runtime.store.list_sessions(
        starred=starred,
        archived=archived,
        limit=limit,
    )
    decorated = [
        (s, await _compute_last_event_at(runtime, s.session_id)) for s in sessions
    ]
    decorated.sort(
        key=lambda item: item[1] or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return decorated


async def _cmd_sessions_list(args: argparse.Namespace) -> int:
    """Print a compact, sorted-by-recency table of sessions.

    Four aligned columns (SESSION_ID, STARTED, LAST, NAME). The
    `--starred` / `--archived` filter flags filter the underlying
    query; they have no columns in the default output.
    """
    runtime = await bootstrap_runtime(root=args.root)
    decorated = await _recent_sessions_decorated(
        runtime,
        starred=(args.starred or None),
        archived=(args.archived or None),
        limit=args.limit,
    )
    if not decorated:
        return 0

    now = datetime.now(UTC)
    sessions = [s for s, _ in decorated]

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


_RESUME_PICKER_LIMIT = 20
"""Cap on the resume-picker session list. Matches the default that
``kolsch sessions list`` shows (its ``--limit`` default) so the
picker and the sessions table stay in step.

Deliberately hard-coded — the picker's job is to disambiguate the
most-recent handful of sessions, not to browse the archive. Users
who need older sessions still have ``kolsch sessions list --limit N``
plus ``kolsch chat --session <id>``.
"""


async def _cmd_chat(args: argparse.Namespace) -> int:
    from cli.chat.app import (  # noqa: PLC0415
        ChatApp,
        force_process_exit,
        register_chat_slash_commands,
    )
    from cli.chat.session import _DEFAULT_AGENT_REF, build_session  # noqa: PLC0415

    # Register ChatApp-owned slash commands (`/export`) on the shared
    # registry only when the chat entry point actually runs. Doing this
    # at module import time would mutate `COMMAND_REGISTRY` for every
    # test that imports `cli.chat`, breaking base-registry-shape
    # assertions (`test_chat_slash`).
    register_chat_slash_commands()

    # --resume flow — resolve a session id via the interactive picker
    # BEFORE build_session runs, then fall through to the unchanged
    # resume path by stamping args.session.
    resume_runtime = await _resolve_resume_session(args)
    if isinstance(resume_runtime, int):
        # Sentinel exit code (conflict, cancel, or empty list) — the
        # helper printed a message and we short-circuit without
        # launching the chat app.
        return resume_runtime

    try:
        session = await build_session(args, runtime=resume_runtime)
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    except PlatformError as exc:
        sys.stderr.write(
            _format_resolve_config_error(
                exc, agent_ref=args.agent_ref or _DEFAULT_AGENT_REF
            )
        )
        return 1

    app = ChatApp(
        session=session,
        engine=session.runtime.engine,
        full_history=getattr(args, "full_history", False),
        auto_approve=getattr(args, "auto_approve", False),
    )
    # bug-116: install a SIGHUP hard-exit before the app runs. If the
    # controlling terminal is torn away (window closed / SSH drop) while
    # the event loop is idle in `select()`, we die immediately instead of
    # letting Textual's NON-daemon input reader busy-loop on the dead tty
    # and later wedge `Py_Finalize`. The terminal is already gone, so
    # skipping its restore is moot.
    _install_chat_hangup_guard()

    # `inline_no_clear=True` bakes the transcript into terminal
    # scrollback on exit — so the conversation stays visible where
    # the user quit the app instead of vanishing.
    await app.run_async(inline=True, inline_no_clear=True)
    code = int(app.return_code or 0)
    # bug-116: hard-exit the moment the app loop returns. The Textual
    # inline driver's input reader is a NON-daemon thread; if it is still
    # spinning on a dead stdin, a normal return would wedge `Py_Finalize`
    # in `threading._shutdown()`'s join and strand the process at 100%
    # CPU. `force_process_exit` (prod `os._exit`) skips that join; under a
    # test exit-hook spy it returns and control falls through to `return`.
    #
    # If `run_async` RAISES instead of returning, this line is skipped and
    # the exception propagates out — but that is not a hang: Textual joins
    # the input thread in `stop_application_mode` (bounded — the reader
    # rechecks `exit_event` every 0.1s) before either swallowing the error
    # into `return_code` or re-raising, so no live non-daemon thread
    # survives to block finalization on that path.
    force_process_exit(code)
    return code


def _install_chat_hangup_guard() -> None:
    """bug-116: hard-exit `kolsch chat` at once on SIGHUP (terminal lost).

    When the controlling terminal goes away the process gets SIGHUP; the
    default action would terminate it, but if some path has left SIGHUP
    handled/blocked the process can instead survive with a dead stdin,
    whose EOF drives Textual's non-daemon input reader into a 100%-CPU
    busy-loop. Installing an explicit hard-exit makes terminal loss
    terminal for the process, immediately, before that spin can start.

    Best-effort and main-thread-only: `signal.signal` raises off the main
    thread and SIGHUP is absent on Windows, so both are swallowed. Routes
    through the same `force_process_exit` seam the post-run path uses, so
    a test can assert the handler fires via `_set_exit_hook` without
    sending a real signal. `128 + signum` is the conventional shell code.

    SIGTERM is deliberately NOT handled: its default disposition already
    terminates the process, and terminal loss delivers SIGHUP (not
    SIGTERM). A hard-exit on SIGTERM would skip Textual's terminal restore
    while the terminal is often still alive (kill/orchestrator), leaving
    it in raw mode — strictly worse than the default.
    """
    import contextlib  # noqa: PLC0415
    import signal  # noqa: PLC0415

    from cli.chat.app import force_process_exit  # noqa: PLC0415

    sighup = getattr(signal, "SIGHUP", None)
    if sighup is None:
        return

    def _on_hangup(signum: int, _frame: Any) -> None:
        force_process_exit(128 + signum)

    # `signal.signal` raises off the main thread / where SIGHUP is
    # unsupported; the post-run `force_process_exit` still backstops the
    # finalize hang, so a failed install is a best-effort no-op.
    with contextlib.suppress(ValueError, OSError):
        signal.signal(sighup, _on_hangup)


async def _resolve_resume_session(
    args: argparse.Namespace,
) -> PlatformRuntime | int | None:
    """Handle the ``--resume`` flag; return early-exit code or a runtime.

    Return values:

    - ``int`` — a sentinel exit code the caller must return without
      launching the chat app. Covers three cases: ``1`` for the
      ``--resume + --session`` conflict, ``0`` for an empty session
      list (nothing to resume), ``0`` for user cancel from the picker.
    - :class:`PlatformRuntime` — the picker bootstrapped a runtime and
      stamped ``args.session``; the caller should pass the runtime
      into :func:`build_session` to avoid a second bootstrap.
    - ``None`` — ``--resume`` was not requested; the caller proceeds
      with its normal (non-resume) startup.
    """
    from cli.session_picker import pick_session  # noqa: PLC0415

    if not getattr(args, "resume", False):
        return None

    if getattr(args, "session", None):
        sys.stderr.write(
            "chat: --resume and --session are mutually exclusive; pick one.\n",
        )
        return 1

    runtime = await bootstrap_runtime(root=args.root)
    rows = await _recent_sessions_decorated(runtime, limit=_RESUME_PICKER_LIMIT)
    if not rows:
        sys.stdout.write("No sessions to resume.\n")
        return 0

    picked = await pick_session(rows)
    if picked is None:
        # Esc / Ctrl-C from the picker — exit cleanly without
        # launching. Matches the "cancel exits 0" acceptance criteria.
        return 0

    args.session = str(picked)
    return runtime


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


async def _cmd_doctor(args: argparse.Namespace) -> int:
    # qol-089 — bundle-drift report. Lazy import so `kolsch --help`
    # doesn't pay the migrator + baseline module-load cost.
    from cli._doctor import cmd_doctor  # noqa: PLC0415

    return await cmd_doctor(args, out=sys.stdout, root=args.root)


async def _cmd_mcp(args: argparse.Namespace) -> int:
    """Dispatch the `mcp` op (spec-105).

    Mirrors ``_cmd_sessions`` / ``_cmd_permissions``: when no op is
    supplied, print the subparser help to stderr and exit 1 rather
    than silently booting a serve loop that a fresh
    ``kolsch mcp --help`` invocation would then hang against.
    """
    if args.mcp_command is None:
        return _print_subcommand_help_and_exit("mcp")
    if args.mcp_command == "serve":
        return await _cmd_mcp_serve(args)
    if args.mcp_command == "bridge":
        return await _cmd_mcp_bridge(args)
    return 1


async def _cmd_mcp_bridge(args: argparse.Namespace) -> int:
    """Serve the MCP tool-bridge proxy on stdio (spec-120).

    A pure socket proxy — it boots NO runtime and touches NO
    workspace; all authority lives in the running kolsch process
    behind ``KOLSCH_MCP_SOCKET``. With the env contract absent it
    exits non-zero without booting anything.
    """
    del args  # the proxy's whole contract is the two env vars
    from tools.bridge import run_bridge_stdio  # noqa: PLC0415

    return await run_bridge_stdio()


async def _cmd_mcp_serve(args: argparse.Namespace) -> int:
    """Serve the MCP operator server on stdio until EOF.

    Boots a `PlatformRuntime` under the workspace guard inherited from
    `main` (`main.py:72-77`), then awaits the FastMCP stdio loop. The
    blocking `run_stdio` call itself is only reachable via the
    real-`claude` e2e round-trip; every non-blocking piece
    (`build_server`, the tool body, the view mirror) is covered by
    in-process tests. `# pragma: no cover` marks the two blocking
    lines so the 85% global floor is not depressed by a line that
    only the e2e path can exercise.
    """
    from mcp_server import run_stdio  # noqa: PLC0415

    runtime = await bootstrap_runtime(root=args.root)
    await run_stdio(runtime)  # pragma: no cover — e2e-only path
    return 0  # pragma: no cover — stdio_server returns on EOF


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
    "doctor": _cmd_doctor,
    "mcp": _cmd_mcp,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _maybe_warn_silent_echo(
    config: ResolvedAgentConfig, runtime: PlatformRuntime
) -> None:
    """Warn on stderr when a real-provider agent would silently echo.

    Fires when:

    - `config.model.provider != "mock"` (the agent intends a real
      call), AND
    - The runtime did not wire `llm_for_model` (i.e. engine has only
      the `EchoLLMDriver` fallback). Normally this only happens when
      no providers are connected; the warning catches any future
      regression that silently routes a real-provider agent through
      the echo path.

    Agents that explicitly use `provider: mock` are silent — that's
    the tutorial path; the user knows what they signed up for.
    """
    if config.model.provider == "mock":
        return
    if runtime.engine._llm_for_model is not None:
        return
    sys.stderr.write(
        f"⚠ this agent specifies provider {config.model.provider!r}, but "
        f"no real-LLM wiring is active — the exchange will silently echo "
        f"via the built-in mock LLM. Run `kolsch connect "
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
