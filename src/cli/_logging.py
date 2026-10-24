"""CLI logging configuration (bug-148).

One function, [`configure_cli_logging`][cli._logging.configure_cli_logging],
owns every `logging` handler the `kolsch` CLI installs. It replaces the
former `logging.basicConfig` call in `cli.main.main`.

Third-party libraries (CrewAI, the OpenAI SDK, httpx, …) log on their
own loggers, and some, like CrewAI's OpenAI completion, log on the
**root** logger. Those records are a presentation problem, not a
kolsch diagnostic: the durable event log already records every failure.
So stderr shows only kolsch's own loggers unless `-v`, and the workspace
log file keeps every record.
"""

from __future__ import annotations

import logging
import logging.handlers
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# kolsch's top-level packages (the `src/*/` directories). Every kolsch
# logger is named under one of them (`cli`, `engine.fallback`,
# `workflows.<run>`, a module's `__name__`, …). A unit test pins this
# against `src/` and `pyproject.toml`'s wheel packages.
KOLSCH_LOGGER_ROOTS: frozenset[str] = frozenset(
    {
        "api",
        "assets",
        "bus",
        "cli",
        "engine",
        "errors",
        "files",
        "kernel",
        "kolsch",
        "llm",
        "mcp_server",
        "platform_runtime",
        "providers",
        "schemas",
        "skills",
        "store",
        "templating",
        "tools",
        "workflows",
    }
)

LOG_FILE_NAME = "kolsch.log"
_LOG_MAX_BYTES = 5_000_000
_LOG_BACKUP_COUNT = 3
_STDERR_FORMAT = "%(levelname)s %(name)s: %(message)s"
_FILE_FORMAT = "%(asctime)s %(process)d %(levelname)s %(name)s: %(message)s"

# Set on every handler this module installs, so a repeat call replaces
# them instead of stacking duplicates (and never touches handlers that
# someone else — e.g. pytest's caplog — installed).
_HANDLER_TAG = "_kolsch_cli_handler"


class KolschLoggerFilter(logging.Filter):
    """Pass only records from a logger under a kolsch top-level package."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return whether `record.name`'s first dotted segment is kolsch's."""
        return record.name.split(".", 1)[0] in KOLSCH_LOGGER_ROOTS


def log_file_path(workspace_root: Path) -> Path:
    """Where the shared CLI log lives: `<workspace>/logs/kolsch.log`."""
    return workspace_root / "logs" / LOG_FILE_NAME


def configure_cli_logging(*, verbose: bool, workspace_root: Path | None) -> None:
    """Install the CLI's stderr and log-file handlers on the root logger.

    - **stderr** — WARNING and above, or INFO and above with `verbose`
      (`kolsch -v`). Without `verbose` only kolsch's own loggers pass
      (see `KOLSCH_LOGGER_ROOTS`); third-party records, including the
      root logger's, are hidden. With `verbose` every record passes.
    - **file** — when `workspace_root` is given (an initialized
      workspace), every record at INFO and above from every logger,
      third-party included, goes to `<workspace>/logs/kolsch.log` with
      timestamp, pid, level and logger name. It is a
      `RotatingFileHandler` (5 MB, 3 backups) shared by every kolsch
      process on the workspace; two processes rotating at once may
      lose or interleave a few lines, which is acceptable for a
      diagnostics file. Opened lazily on the first record.
    - **no file** — `workspace_root=None` (uninitialized workspace:
      `setup`, `doctor`, `mcp bridge`), or a `logs/` that cannot be
      created or opened, means stderr only. Logging never makes a
      command fail.

    The root logger is set to INFO so the file sees INFO records even
    when stderr shows only WARNING. Idempotent: handlers installed by
    an earlier call are removed first.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_TAG, False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(logging.INFO)

    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(logging.INFO if verbose else logging.WARNING)
    stderr_handler.setFormatter(logging.Formatter(_STDERR_FORMAT))
    if not verbose:
        stderr_handler.addFilter(KolschLoggerFilter())
    _install(root, stderr_handler)

    if workspace_root is not None:
        file_handler = _open_file_handler(workspace_root)
        if file_handler is not None:
            _install(root, file_handler)


def _install(root: logging.Logger, handler: logging.Handler) -> None:
    setattr(handler, _HANDLER_TAG, True)
    root.addHandler(handler)


def _open_file_handler(workspace_root: Path) -> logging.Handler | None:
    """A rotating handler on the workspace log, or `None` if unusable.

    `delay=True` defers the open to the first record, so an unwritable
    file would only fail at emit time (where `logging` reports and
    swallows it). Probe-open now instead, so the no-file case is
    decided here and a broken `logs/` never prints logging errors.
    """
    path = log_file_path(workspace_root)
    try:
        path.parent.mkdir(exist_ok=True)
        with path.open("a", encoding="utf-8"):
            pass
    except OSError:
        return None
    handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
        delay=True,
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(_FILE_FORMAT))
    return handler
