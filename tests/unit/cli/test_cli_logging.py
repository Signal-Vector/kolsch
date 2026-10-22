"""bug-148 Test 4 — `configure_cli_logging`.

stderr shows only kolsch loggers (WARNING+) unless verbose; the
workspace `logs/kolsch.log` gets every record at INFO+; a missing or
unwritable `logs/` never raises; repeat calls don't stack handlers; and
`KOLSCH_LOGGER_ROOTS` matches the real package list.
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path

import pytest

from cli._logging import KOLSCH_LOGGER_ROOTS, configure_cli_logging, log_file_path

_REPO = Path(__file__).resolve().parents[3]
_TAG = "_kolsch_cli_handler"


def _emit_all() -> None:
    logging.getLogger().warning("root-warning")
    logging.getLogger("crewai.foo").warning("crewai-warning")
    logging.getLogger("engine.fallback").warning("engine-warning")
    logging.getLogger("cli").error("cli-error")
    logging.getLogger("cli").info("cli-info")
    logging.getLogger("httpx").info("httpx-info")


_WARNINGS = ["root-warning", "crewai-warning", "engine-warning", "cli-error"]


def _tagged() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if getattr(h, _TAG, False)]


def test_cli_logging_stderr_hides_third_party_without_verbose(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_cli_logging(verbose=False, workspace_root=None)
    _emit_all()

    err = capsys.readouterr().err
    assert "root-warning" not in err
    assert "crewai-warning" not in err
    assert "WARNING engine.fallback: engine-warning" in err
    assert "ERROR cli: cli-error" in err
    assert "cli-info" not in err  # WARNING threshold
    assert "httpx-info" not in err


def test_cli_logging_stderr_shows_everything_with_verbose(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_cli_logging(verbose=True, workspace_root=None)
    _emit_all()

    err = capsys.readouterr().err
    for message in [*_WARNINGS, "cli-info", "httpx-info"]:
        assert message in err, message
    assert "WARNING root: root-warning" in err


def test_cli_logging_file_gets_every_record_at_info(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "logs").mkdir()
    configure_cli_logging(verbose=False, workspace_root=tmp_path)
    _emit_all()
    for handler in _tagged():
        handler.flush()

    text = log_file_path(tmp_path).read_text(encoding="utf-8")
    for message in [*_WARNINGS, "cli-info", "httpx-info"]:
        assert message in text, message
    # Timestamp, pid, level, logger name.
    line = next(ln for ln in text.splitlines() if "crewai-warning" in ln)
    assert f" {os.getpid()} WARNING crewai.foo: crewai-warning" in line
    assert line[:4].isdigit()
    capsys.readouterr()


def test_cli_logging_creates_log_file_and_logs_dir(tmp_path: Path) -> None:
    # The `kolsch run` internal-error hint points at this file.
    configure_cli_logging(verbose=False, workspace_root=tmp_path)
    assert log_file_path(tmp_path).is_file()


def test_cli_logging_records_tracebacks_in_file(tmp_path: Path) -> None:
    configure_cli_logging(verbose=False, workspace_root=tmp_path)
    try:
        raise ValueError("kaboom")
    except ValueError as exc:
        logging.getLogger("cli").info("failed", exc_info=exc)
    for handler in _tagged():
        handler.flush()

    text = log_file_path(tmp_path).read_text(encoding="utf-8")
    assert "Traceback (most recent call last)" in text
    assert "ValueError: kaboom" in text


def test_cli_logging_no_workspace_creates_no_file(tmp_path: Path) -> None:
    configure_cli_logging(verbose=False, workspace_root=None)
    _emit_all()
    assert not (tmp_path / "logs").exists()
    assert not any(isinstance(h, logging.FileHandler) for h in _tagged())


def test_cli_logging_logs_is_a_file_does_not_raise(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "logs").write_text("not a directory")
    configure_cli_logging(verbose=False, workspace_root=tmp_path)
    _emit_all()

    assert not any(isinstance(h, logging.FileHandler) for h in _tagged())
    assert "Logging error" not in capsys.readouterr().err


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_cli_logging_unwritable_logs_dir_does_not_raise(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    logs.chmod(0o500)
    try:
        configure_cli_logging(verbose=False, workspace_root=tmp_path)
        _emit_all()
    finally:
        logs.chmod(0o700)

    assert not any(isinstance(h, logging.FileHandler) for h in _tagged())
    assert "Logging error" not in capsys.readouterr().err


def test_cli_logging_is_idempotent(tmp_path: Path) -> None:
    configure_cli_logging(verbose=False, workspace_root=tmp_path)
    configure_cli_logging(verbose=True, workspace_root=tmp_path)

    handlers = _tagged()
    assert len(handlers) == 2
    assert sum(isinstance(h, logging.FileHandler) for h in handlers) == 1


def test_cli_logging_leaves_foreign_handlers_alone() -> None:
    foreign = logging.NullHandler()
    root = logging.getLogger()
    root.addHandler(foreign)
    try:
        configure_cli_logging(verbose=False, workspace_root=None)
        configure_cli_logging(verbose=False, workspace_root=None)
        assert foreign in root.handlers
    finally:
        root.removeHandler(foreign)


def test_cli_logging_roots_match_packages() -> None:
    # Every `src/<pkg>/__init__.py` is a kolsch top-level package.
    src_packages = {p.parent.name for p in (_REPO / "src").glob("*/__init__.py")}
    assert src_packages == KOLSCH_LOGGER_ROOTS

    # pyproject's wheel packages are all covered. NOTE: `src/workflows`
    # is currently missing from that list (a packaging gap tracked
    # separately), so this is a subset check, not equality.
    pyproject = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    wheel_packages = {Path(entry).name for entry in wheel}
    assert wheel_packages <= KOLSCH_LOGGER_ROOTS
