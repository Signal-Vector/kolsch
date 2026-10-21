"""bug-147 — the run-once seam (`_run_agent_once` / `_RunOutcome`).

`kolsch run` (`_cmd_run`) and `kolsch setup`'s Step E test run both go
through `_run_agent_once`. These tests pin:

- the core's outcome on engine-Fatal (`AgentExecutionError`), on an
  unexpected exception, on success, and on a `--stream` run that ends
  Fatal — and that the core itself logs nothing;
- `_cmd_run`'s unchanged rendering: exit 2 + one `cli` ERROR record
  `agent execution failed: …` with `exc_info` on failure, the agent
  message JSON on success;
- the PRODUCTION setup run handler (`make_default_setup_io`) now
  receives the failed run's session id and durable error envelope —
  the root cause of bug-147 was that it never did.

Stub runtime only — no LLM, no store I/O.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from cli._setup import _TestRunResult, make_default_setup_io
from cli.main import _cmd_run, _run_agent_once
from engine import AgentExecutionError
from schemas import (
    Actor,
    AgentId,
    Event,
    EventId,
    EventType,
    IOSpec,
    ModelSpec,
    ResolvedAgentConfig,
    SessionId,
    ThreadId,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    import pytest

# `cli/__init__.py` exports the `main` FUNCTION, shadowing the `cli.main`
# submodule as a package attribute — reach the real module via sys.modules
# to patch its `bootstrap_runtime` global (see test_run_model_override.py).
_MAIN = sys.modules[_cmd_run.__module__]

_SESSION_ID = SessionId(f"ses_{uuid4()}")
_THREAD_ID = ThreadId(f"th_{uuid4()}")

_ENVELOPE: dict[str, Any] = {
    "type": "platform.internal_error",
    "message": "Internal platform error: openai.BadRequestError",
    "where": "engine.execute.boundary",
    "detail": {
        "original_type": "openai.BadRequestError",
        "args": ["Error code: 400 - MissingSessionID: request is missing a header"],
    },
    "cause": None,
}


def _event(event_type: EventType, data: dict[str, Any]) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        thread_id=_THREAD_ID,
        session_id=_SESSION_ID,
        type=event_type,
        data=data,
        actor=Actor(kind="agent", id="ag_x"),
        created_at=datetime.now(UTC),
    )


def _failed_event() -> Event:
    return _event(EventType.THREAD_EXCHANGE_FAILED, {"error": dict(_ENVELOPE)})


def _config() -> ResolvedAgentConfig:
    return ResolvedAgentConfig(
        agent_id=AgentId("ag_x"),
        name="x",
        description="d",
        model=ModelSpec(provider="mock", model="echo"),
        prompt="You are an agent.",
        io=IOSpec(mode="conversational"),
    )


class _Session:
    session_id = _SESSION_ID


class _Thread:
    thread_id = _THREAD_ID


class _Store:
    async def create_session(self, agent_id: object, name: object = None) -> object:
        return _Session()

    async def list_threads(self, session_id: object) -> list[object]:
        return [_Thread()]


class _Engine:
    """`execute` raises `raises` (or returns an agent message);
    `execute_stream` yields `stream` events."""

    def __init__(
        self, *, raises: Exception | None = None, stream: list[Event] | None = None
    ) -> None:
        self._raises = raises
        self._stream = stream or []

    async def execute(self, config: object, ctx: object) -> Event:
        if self._raises is not None:
            raise self._raises
        return _event(EventType.AGENT_MESSAGE, {"text": "hi"})

    async def execute_stream(self, config: object, ctx: object) -> AsyncIterator[Event]:
        for event in self._stream:
            yield event


class _RT:
    def __init__(self, engine: _Engine) -> None:
        self.llm_providers_config: dict[str, object] = {}
        self.engine = engine
        self.store = _Store()

    async def build_resolved_config(
        self, ref: str, call_overrides: object = None
    ) -> ResolvedAgentConfig:
        return _config()


def _install(monkeypatch: pytest.MonkeyPatch, engine: _Engine) -> None:
    async def _fake_bootstrap(root: object = None) -> object:
        return _RT(engine)

    monkeypatch.setattr(_MAIN, "bootstrap_runtime", _fake_bootstrap)


def _args(*, stream: bool = False, root: object = None) -> argparse.Namespace:
    # Step-E-shaped (see `_step_e_test_run`) — no `model` / `skill` attrs.
    return argparse.Namespace(
        root=root,
        agent_ref="kolsch/agents/helper",
        input=["content=hello"],
        stream=stream,
        session=None,
    )


def _run_setup_handler(root: Path, args: argparse.Namespace) -> _TestRunResult:
    async def _call() -> _TestRunResult:
        return await make_default_setup_io(root=root).run_handler(args)

    return asyncio.run(_call())


def _error_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "cli" and r.levelno >= logging.ERROR]


# ---------------------------------------------------------------------------
# The core
# ---------------------------------------------------------------------------


def test_core_engine_fatal_carries_session_and_failed_event(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    failed = _failed_event()
    exc = AgentExecutionError(failed)
    _install(monkeypatch, _Engine(raises=exc))
    caplog.set_level(logging.DEBUG)

    outcome = asyncio.run(_run_agent_once(_args()))

    assert outcome.rc == 2
    assert outcome.session_id == _SESSION_ID
    assert outcome.failed_event is failed
    assert outcome.exc is exc
    assert outcome.agent_message is None
    assert _error_records(caplog) == []  # the core logs nothing


def test_core_unexpected_exception_sets_only_exc(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    boom = ValueError("boom")
    _install(monkeypatch, _Engine(raises=boom))
    caplog.set_level(logging.DEBUG)

    outcome = asyncio.run(_run_agent_once(_args()))

    assert outcome.rc == 2
    assert outcome.session_id == _SESSION_ID
    assert outcome.failed_event is None
    assert outcome.exc is boom
    assert _error_records(caplog) == []


def test_core_success_returns_agent_message_without_printing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, _Engine())

    outcome = asyncio.run(_run_agent_once(_args()))

    assert outcome.rc == 0
    assert outcome.session_id == _SESSION_ID
    assert outcome.agent_message is not None
    assert outcome.agent_message.data == {"text": "hi"}
    assert outcome.failed_event is None
    assert outcome.exc is None
    assert capsys.readouterr().out == ""  # the wrapper prints the JSON


def test_core_stream_fatal_records_failed_event(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    failed = _failed_event()
    _install(monkeypatch, _Engine(stream=[failed]))

    outcome = asyncio.run(_run_agent_once(_args(stream=True)))

    assert outcome.rc == 2
    assert outcome.session_id == _SESSION_ID
    assert outcome.failed_event is failed
    assert outcome.exc is None  # execute_stream never raises on Fatal
    # The stream path still writes the event line to stdout, as today.
    assert "thread.exchange_failed" in capsys.readouterr().out


def test_core_stream_success_rc_0(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _Engine(stream=[_event(EventType.AGENT_MESSAGE, {})]))

    outcome = asyncio.run(_run_agent_once(_args(stream=True)))

    assert outcome.rc == 0
    assert outcome.failed_event is None


# ---------------------------------------------------------------------------
# `kolsch run` — rendering unchanged
# ---------------------------------------------------------------------------


def test_cmd_run_engine_fatal_logs_one_cli_error_with_exc_info(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exc = AgentExecutionError(_failed_event())
    _install(monkeypatch, _Engine(raises=exc))
    caplog.set_level(logging.DEBUG)

    rc = asyncio.run(_cmd_run(_args()))

    assert rc == 2
    records = _error_records(caplog)
    assert len(records) == 1
    assert records[0].getMessage().startswith("agent execution failed:")
    assert records[0].exc_info is not None
    assert records[0].exc_info[1] is exc
    assert capsys.readouterr().out == ""


def test_cmd_run_success_prints_agent_message_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, _Engine())

    rc = asyncio.run(_cmd_run(_args()))

    assert rc == 0
    assert capsys.readouterr().out == json.dumps({"text": "hi"}, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# The PRODUCTION setup run handler (Test 1 — the bug-147 root cause)
# ---------------------------------------------------------------------------


def test_setup_run_handler_surfaces_session_id_and_durable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    failed = _failed_event()
    _install(monkeypatch, _Engine(raises=AgentExecutionError(failed)))
    caplog.set_level(logging.DEBUG)

    result = _run_setup_handler(tmp_path, _args(root=tmp_path))

    assert result.rc == 2
    assert result.session_id == str(failed.session_id)
    assert result.error is not None
    assert result.error["type"] == "platform.internal_error"
    assert result.error["message"] == _ENVELOPE["message"]
    # D1: the durable event + banner carry an engine-Fatal failure — no
    # traceback is logged on this path.
    assert _error_records(caplog) == []


def test_setup_run_handler_logs_unexpected_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    boom = ValueError("boom")
    _install(monkeypatch, _Engine(raises=boom))
    caplog.set_level(logging.DEBUG)

    result = _run_setup_handler(tmp_path, _args())

    assert result.rc == 2
    assert result.session_id == str(_SESSION_ID)
    assert result.error is None
    records = _error_records(caplog)
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert records[0].exc_info[1] is boom


def test_setup_run_handler_success_echoes_agent_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install(monkeypatch, _Engine())

    result = _run_setup_handler(tmp_path, _args())

    assert result.rc == 0
    assert result.error is None
    # Unchanged from before bug-147: setup's output shows the helper's
    # JSON reply line, as `kolsch run` prints it.
    assert capsys.readouterr().out == json.dumps({"text": "hi"}, sort_keys=True) + "\n"
