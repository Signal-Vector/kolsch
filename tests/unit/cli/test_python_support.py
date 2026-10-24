"""bug-155 — the first-import interpreter guard and its pyproject drift check.

uv ignores `Requires-Python` upper bounds, so `cli/__init__.py` runs
`ensure_supported_python()` before importing crewai (whose pinned
chromadb crashes on 3.14). These tests pin the supported range, the
message, and that the range stays equal to `pyproject.toml`.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

from cli import _python_support
from cli._python_support import (
    ensure_supported_python,
    requires_python_specifier,
    unsupported_python_message,
)

_PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"


@pytest.mark.parametrize("version", [(3, 12, 0), (3, 13, 9)])
def test_supported_versions_pass(version: tuple[int, ...]) -> None:
    assert unsupported_python_message(version) is None
    ensure_supported_python(version)


@pytest.mark.parametrize("version", [(3, 14, 0), (3, 15, 0), (3, 11, 9)])
def test_unsupported_versions_exit_with_message(version: tuple[int, ...]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        ensure_supported_python(version)
    message = excinfo.value.code
    # A str code means Python prints it to stderr and exits with status 1.
    assert isinstance(message, str)
    assert ".".join(map(str, version)) in message
    assert sys.executable in message
    assert "3.12" in message
    assert "3.13" in message
    assert "uv tool install --reinstall --python 3.13 kolsch" in message
    assert "\n" not in message


def test_defaults_to_running_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    # The test suite itself runs on a supported interpreter.
    ensure_supported_python()
    monkeypatch.setattr(_python_support.sys, "version_info", (3, 14, 0, "final", 0))
    with pytest.raises(SystemExit, match=r"Python 3\.14\.0 "):
        ensure_supported_python()


def test_range_matches_pyproject_requires_python() -> None:
    with _PYPROJECT.open("rb") as fh:
        declared = tomllib.load(fh)["project"]["requires-python"]
    expected = requires_python_specifier()
    assert declared == expected, (
        f"pyproject.toml requires-python {declared!r} != guard range "
        f"{expected!r} (cli/_python_support.py); update both together"
    )
