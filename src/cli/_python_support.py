"""First-import interpreter guard for the `kolsch` CLI (bug-155).

kolsch supports Python 3.12 and 3.13 only. The pinned `crewai==1.14.6`
declares `Requires-Python <3.14`, and on 3.14 its `chromadb` 1.1.1
dependency crashes at import time (its `Settings` is built on
`pydantic.v1`, which raises `ConfigError` on 3.14). `pyproject.toml`
mirrors that bound in `requires-python`, which makes pip refuse the
install with a clear message — but uv (the documented install path,
`uv tool install kolsch`) ignores `Requires-Python` upper bounds and
installs into 3.14 anyway. Without this guard the user then gets a
60-line chromadb traceback from `kolsch --help`.

`cli/__init__.py` calls `ensure_supported_python()` before importing
anything else from kolsch, so this module must stay stdlib-only. The
range here is kept equal to `pyproject.toml` by a drift test
(`tests/unit/cli/test_python_support.py`); lift both together with the
crewai 1.15.x bump follow-up.
"""

from __future__ import annotations

import sys

MIN_SUPPORTED: tuple[int, int] = (3, 12)
"""Oldest supported `(major, minor)`, inclusive."""

MAX_EXCLUSIVE: tuple[int, int] = (3, 14)
"""First unsupported `(major, minor)`."""


def _fmt(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def requires_python_specifier() -> str:
    """Return the `requires-python` string this range corresponds to."""
    return f">={_fmt(MIN_SUPPORTED)},<{_fmt(MAX_EXCLUSIVE)}"


def unsupported_python_message(version_info: tuple[int, ...]) -> str | None:
    """Return the user-facing error for an unsupported interpreter.

    Returns `None` when `version_info` is inside the supported range.
    """
    if MIN_SUPPORTED <= tuple(version_info[:2]) < MAX_EXCLUSIVE:
        return None
    newest = (MAX_EXCLUSIVE[0], MAX_EXCLUSIVE[1] - 1)
    supported = f"{_fmt(MIN_SUPPORTED)}-{_fmt(newest)}"
    return (
        f"kolsch: Python {_fmt(version_info[:3])} ({sys.executable}) is not "
        f"supported; kolsch requires Python {supported}. Reinstall it on a "
        f"supported interpreter: `uv tool install --reinstall --python "
        f"{_fmt(newest)} kolsch` (or, with pip, create a Python "
        f"{_fmt(MIN_SUPPORTED)}/{_fmt(newest)} virtualenv and install "
        f"kolsch into it)."
    )


def ensure_supported_python(version_info: tuple[int, ...] | None = None) -> None:
    """Exit with a one-paragraph message if the interpreter is unsupported.

    Raises `SystemExit` with the message as its code, so Python prints
    it to stderr without a traceback and exits with status 1.
    """
    if version_info is None:
        version_info = tuple(sys.version_info[:3])
    message = unsupported_python_message(version_info)
    if message is not None:
        raise SystemExit(message)
