"""bug-148 Test 2 — `kolsch run` failure paths + setup's test-run plumbing.

In-process against a stub runtime (no LLM, no store I/O). Pins:

- engine Fatal (`AgentExecutionError`): rc 2, nothing on stdout, the
  failure block on stderr, no `Traceback` unless `-v` (INFO logging);
- an unexpected engine exception: rc 2, the internal-error line + the
  `kolsch -v` hint;
- pre-flight `--session` failures (malformed, unknown, no threads): rc
  2, one `error:` line (+ hint), no traceback, a structured
  `outcome.preflight`;
- setup's production `_run_handler` reports the real error code, and
  the banner's `Cause:` line shows it; a pre-flight failure's banner no
  longer claims an exchange failed.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING
from uuid import uuid4

from cli._setup import make_default_setup_io, run_setup
from cli.main import _cmd_run, _run_agent_once
from engine import AgentExecutionError
from errors import ConfigInvalid, PlatformError, SessionNotFound
from tests.component.test_setup import _make_test_io
from tests.unit.cli.test_run_agent_once import (
    _ENVELOPE,
    _SESSION_ID,
    _config,
    _Engine,
    _failed_event,
    _run_setup_handler,
    _Session,
    _Thread,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import pytest

    from schemas import ResolvedAgentConfig, SessionId

_MAIN = sys.modules[_cmd_run.__module__]
_AGENT = "kolsch/agents/helper"


class _Store:
    """`get_session` knows only `_SESSION_ID`; `threads` is configurable."""

    def __init__(self, *, threads: bool = True) -> None:
        self._threads = threads

    async def create_session(self, agent_id: object, name: object = None) -> object:
        return _Session()

    async def get_session(self, session_id: SessionId) -> object:
        if session_id != _SESSION_ID:
            raise SessionNotFound(str(session_id))
        return _Session()

    async def list_threads(self, session_id: object) -> list[object]:
        return [_Thread()] if self._threads else []


class _RT:
    def __init__(
        self,
        engine: _Engine,
        *,
        threads: bool = True,
        resolve_error: PlatformError | None = None,
    ) -> None:
        self.llm_providers_config: dict[str, object] = {}
        self.engine = engine
        self.store = _Store(threads=threads)
        self._resolve_error = resolve_error

    async def build_resolved_config(
        self, ref: str, call_overrides: object = None
    ) -> ResolvedAgentConfig:
        if self._resolve_error is not None:
            raise self._resolve_error
        return _config()


def _install(
    monkeypatch: pytest.MonkeyPatch,
    engine: _Engine,
    *,
    threads: bool = True,
    resolve_error: PlatformError | None = None,
) -> None:
    async def _fake_bootstrap(root: object = None) -> object:
        return _RT(engine, threads=threads, resolve_error=resolve_error)

    monkeypatch.setattr(_MAIN, "bootstrap_runtime", _fake_bootstrap)


def _args(
    *, root: Path | None = None, session: str | None = None
) -> argparse.Namespace:
    return argparse.Namespace(
        root=root,
        agent_ref=_AGENT,
        input=["content=hello"],
        stream=False,
        session=session,
    )


@contextmanager
def _cli_stderr_logging(*, verbose: bool) -> Iterator[None]:
    """Mimic `main()`'s stderr logging: WARNING, or INFO under `-v`.

    Built inside the test so the handler binds capsys's `sys.stderr`.
    """
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    level = logging.INFO if verbose else logging.WARNING
    handler.setLevel(level)
    previous = root.level
    root.addHandler(handler)
    root.setLevel(level)
    try:
        yield
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


# ---------------------------------------------------------------------------
# Engine-side failures
# ---------------------------------------------------------------------------


def test_run_failure_engine_fatal_prints_block_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    failed = _failed_event()
    _install(monkeypatch, _Engine(raises=AgentExecutionError(failed)))

    with _cli_stderr_logging(verbose=False):
        rc = asyncio.run(_cmd_run(_args()))

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert captured.err.splitlines() == [
        f"error: {_AGENT} failed — platform.internal_error: {_ENVELOPE['message']}",
        f"  cause:   {_ENVELOPE['detail']['args'][0]}",
        f"  session: {failed.session_id}",
        f"  inspect: kolsch sessions events {failed.session_id}",
    ]
    assert "Traceback" not in captured.err


def test_run_failure_engine_fatal_verbose_adds_traceback_after_block(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    failed = _failed_event()
    _install(monkeypatch, _Engine(raises=AgentExecutionError(failed)))

    with _cli_stderr_logging(verbose=True):
        rc = asyncio.run(_cmd_run(_args()))

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    block_end = captured.err.index(f"kolsch sessions events {failed.session_id}")
    traceback_at = captured.err.index("Traceback (most recent call last)")
    assert block_end < traceback_at
    assert "AgentExecutionError" in captured.err[traceback_at:]


def test_run_failure_unexpected_exception_is_internal_error_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, _Engine(raises=ValueError("boom")))

    with _cli_stderr_logging(verbose=False):
        rc = asyncio.run(_cmd_run(_args(root=tmp_path)))

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert captured.err.splitlines() == [
        f"error: {_AGENT} failed with an internal error (ValueError): boom",
        "  re-run with `kolsch -v` for the traceback; full details in "
        f"{tmp_path.resolve() / 'logs' / 'kolsch.log'}",
    ]


def test_run_failure_unexpected_exception_verbose_has_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, _Engine(raises=ValueError("boom")))

    with _cli_stderr_logging(verbose=True):
        rc = asyncio.run(_cmd_run(_args(root=tmp_path)))

    err = capsys.readouterr().err
    assert rc == 2
    assert "Traceback (most recent call last)" in err
    assert "ValueError: boom" in err


# ---------------------------------------------------------------------------
# Pre-flight `--session` failures
# ---------------------------------------------------------------------------


def _assert_one_error_line(err: str) -> None:
    assert "Traceback" not in err
    assert [ln for ln in err.splitlines() if ln.startswith("error:")] == [
        err.splitlines()[0]
    ]


def test_run_failure_malformed_session_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, _Engine())

    with _cli_stderr_logging(verbose=False):
        rc = asyncio.run(_cmd_run(_args(session="not-an-id")))
        captured = capsys.readouterr()
        outcome = asyncio.run(_run_agent_once(_args(session="not-an-id")))

    assert rc == outcome.rc == 2
    assert captured.out == ""
    _assert_one_error_line(captured.err)
    assert captured.err.startswith("error: invalid --session 'not-an-id': ")
    assert "kolsch sessions list" in captured.err
    assert outcome.preflight is not None
    assert outcome.preflight.code is None
    assert outcome.session_id is None
    assert outcome.exc is None


def test_run_failure_unknown_session_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unknown = f"ses_{uuid4()}"
    _install(monkeypatch, _Engine())

    with _cli_stderr_logging(verbose=False):
        outcome = asyncio.run(_run_agent_once(_args(session=unknown)))

    err = capsys.readouterr().err
    assert outcome.rc == 2
    _assert_one_error_line(err)
    assert err.startswith(f"error: session.not_found: Session {unknown!r} not found")
    assert "kolsch sessions list" in err
    assert outcome.preflight is not None
    assert outcome.preflight.code == "session.not_found"
    assert outcome.session_id is None


def test_run_failure_session_without_threads(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, _Engine(), threads=False)

    with _cli_stderr_logging(verbose=False):
        outcome = asyncio.run(_run_agent_once(_args(session=str(_SESSION_ID))))

    captured = capsys.readouterr()
    assert outcome.rc == 2
    assert captured.out == ""
    _assert_one_error_line(captured.err)
    assert captured.err.startswith(f"error: session {_SESSION_ID} has no threads\n")
    assert outcome.preflight is not None
    assert outcome.session_id == _SESSION_ID


def test_run_failure_known_session_still_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, _Engine())

    rc = asyncio.run(_cmd_run(_args(session=str(_SESSION_ID))))

    assert rc == 0
    assert capsys.readouterr().out.strip() == '{"text": "hi"}'


# ---------------------------------------------------------------------------
# Setup's test run (the production `_run_handler`)
# ---------------------------------------------------------------------------


async def _setup_with_production_handler(tmp_path: Path) -> tuple[int, str]:
    out = io.StringIO()
    production = make_default_setup_io(root=tmp_path)
    io_ = _make_test_io(root=tmp_path, out=out, run_handler=production.run_handler)
    rc = await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    return rc, out.getvalue()


def _clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)


def test_setup_test_run_failure_code_reaches_handler_and_banner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_provider_env(monkeypatch)
    failed = _failed_event()
    _install(monkeypatch, _Engine(raises=AgentExecutionError(failed)))

    result = _run_setup_handler(tmp_path, _args(root=tmp_path))
    assert (result.rc, (result.error or {}).get("type")) == (
        2,
        "platform.internal_error",
    )
    assert result.preflight is None

    rc, out = asyncio.run(_setup_with_production_handler(tmp_path))

    assert rc != 0
    assert "Cause:     platform.internal_error — " in out
    assert f"Session:   {failed.session_id}" in out


def test_setup_test_run_failure_code_preflight_says_run_did_not_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Coordinator note (bug-147 final QA): a config-resolve failure ran
    # no exchange, so the banner must not say "test exchange failed".
    _clear_provider_env(monkeypatch)
    _install(
        monkeypatch,
        _Engine(),
        resolve_error=ConfigInvalid(
            [{"path": "defaults.model", "message": "is missing"}]
        ),
    )

    rc, out = asyncio.run(_setup_with_production_handler(tmp_path))

    assert rc != 0
    cause = [ln for ln in out.splitlines() if ln.startswith("Cause:")]
    assert cause == [
        "Cause:     config.invalid — the test run did not start: could not "
        f"resolve agent '{_AGENT}': Configuration validation failed with 1 error(s)"
    ]
    assert "test exchange failed" not in out
    assert "kolsch sessions list" not in out
    assert "kolsch doctor" in out
