"""Concise stderr rendering of a failed `kolsch run` (bug-148).

Pure formatters — no I/O, no logging. `kolsch run` writes their output
to stderr; the traceback is a separate `-v`-only concern of the caller.

- [`format_exchange_failure`][cli._run_failure.format_exchange_failure]
  renders the durable `thread.exchange_failed` event an ordinary
  engine-Fatal failure carries (`AgentExecutionError.failed_event`).
- [`format_internal_failure`][cli._run_failure.format_internal_failure]
  renders an exception that is NOT an `AgentExecutionError` — a kolsch
  bug, not an ordinary failure.
- [`one_line`][cli._run_failure.one_line] is the shared one-line
  flattener (also used by `kolsch setup`'s failure banner).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from schemas import Event

# The `cause:` line's length cap, `…` included.
CAUSE_MAX_CHARS = 300


def one_line(text: str, limit: int = CAUSE_MAX_CHARS) -> str:
    """Flatten `text` to one line of at most `limit` characters.

    Whitespace runs (newlines included) collapse to single spaces; a
    longer result is cut and ends with `…` (counted in `limit`).
    """
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _cause_text(error: dict[object, object]) -> str | None:
    """The one-line cause of a durable error envelope, or `None`.

    Prefers `error["cause"]` (a nested envelope: `type: message`), then
    the wrapped exception's own text in `error["detail"]["args"][0]`.
    """
    cause = error.get("cause")
    if isinstance(cause, dict):
        parts = [
            str(part)
            for part in (cause.get("type"), cause.get("message"))
            if part is not None and str(part).strip()
        ]
        if parts:
            return ": ".join(parts)
    detail = error.get("detail")
    if isinstance(detail, dict):
        args = detail.get("args")
        if isinstance(args, list) and args and str(args[0]).strip():
            return str(args[0])
    return None


def format_exchange_failure(failed_event: Event, *, agent_ref: str) -> str:
    """Render an engine-Fatal exchange as a short stderr block.

    `failed_event` is the durable `thread.exchange_failed` event; its
    `data["error"]` is the canonical error envelope (`type`, `message`,
    `where`, `detail`, `cause`). The block is, in order:

        error: <agent_ref> failed — <type>: <message>
          cause:   <one line, ≤ CAUSE_MAX_CHARS>   (omitted when absent)
          session: <session id>
          inspect: kolsch sessions events <session id>

    Total: a missing or malformed envelope falls back to the header
    `error: <agent_ref> failed (event <event_id>)` (no `cause:` line)
    and this never raises. The output ends with a newline. The
    formatter is code-agnostic — it renders whatever `type` the engine
    classified.
    """
    error = failed_event.data.get("error")
    header = f"error: {agent_ref} failed (event {failed_event.event_id})"
    cause: str | None = None
    if isinstance(error, dict):
        code = error.get("type")
        message = error.get("message")
        if isinstance(code, str) and code and isinstance(message, str):
            header = f"error: {agent_ref} failed — {code}: {' '.join(message.split())}"
            cause = _cause_text(error)
    lines = [header]
    if cause is not None:
        lines.append(f"  cause:   {one_line(cause)}")
    session_id = failed_event.session_id
    lines.append(f"  session: {session_id}")
    lines.append(f"  inspect: kolsch sessions events {session_id}")
    return "\n".join(lines) + "\n"


def format_internal_failure(
    exc: BaseException, *, agent_ref: str, workspace_root: Path
) -> str:
    """Render an unexpected (non-`AgentExecutionError`) exception.

    Such an exception escaping `engine.execute` is a kolsch bug, so the
    block says so and points at where the traceback lives:

        error: <agent_ref> failed with an internal error (<Type>): <message>
          re-run with `kolsch -v` for the traceback; full details in
          <workspace>/logs/kolsch.log            (one line)
    """
    message = one_line(str(exc)) or "(no message)"
    log_path = workspace_root / "logs" / "kolsch.log"
    return (
        f"error: {agent_ref} failed with an internal error "
        f"({type(exc).__name__}): {message}\n"
        f"  re-run with `kolsch -v` for the traceback; full details in {log_path}\n"
    )
