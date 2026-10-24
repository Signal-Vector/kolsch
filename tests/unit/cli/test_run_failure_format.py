"""bug-148 Test 1 — the `kolsch run` failure formatters are pure and total.

`format_exchange_failure` renders the durable `thread.exchange_failed`
event as a fixed four-line block; `format_internal_failure` renders an
unexpected exception as two lines. Neither raises on malformed input.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from cli._run_failure import (
    CAUSE_MAX_CHARS,
    format_exchange_failure,
    format_internal_failure,
    one_line,
)
from schemas import Actor, Event, EventId, EventType, SessionId, ThreadId

if TYPE_CHECKING:
    from pathlib import Path

_AGENT = "kolsch/agents/helper"
_SESSION_ID = SessionId(f"ses_{uuid4()}")
_EVENT_ID = EventId(f"ev_{uuid4()}")
_400_TEXT = (
    "Error code: 400 - {'type': 'error', 'error': {'type': 'MissingSessionID', "
    "'message': 'request is missing a session header'}}"
)

# The 2026-10-21 incident's durable envelope.
_INCIDENT_ERROR: dict[str, Any] = {
    "type": "platform.internal_error",
    "message": "Internal platform error: openai.BadRequestError",
    "where": "engine.execute.boundary",
    "cause": None,
    "detail": {"original_type": "openai.BadRequestError", "args": [_400_TEXT]},
}


def _failed(data: dict[str, Any]) -> Event:
    return Event(
        event_id=_EVENT_ID,
        thread_id=ThreadId(f"th_{uuid4()}"),
        session_id=_SESSION_ID,
        type=EventType.THREAD_EXCHANGE_FAILED,
        data=data,
        actor=Actor(kind="agent", id="ag_x"),
        created_at=datetime.now(UTC),
    )


def _tail() -> list[str]:
    return [
        f"  session: {_SESSION_ID}",
        f"  inspect: kolsch sessions events {_SESSION_ID}",
    ]


def test_exchange_failure_incident_payload_is_the_four_line_block() -> None:
    out = format_exchange_failure(_failed({"error": _INCIDENT_ERROR}), agent_ref=_AGENT)

    assert out.endswith("\n")
    assert out.splitlines() == [
        f"error: {_AGENT} failed — platform.internal_error: "
        "Internal platform error: openai.BadRequestError",
        f"  cause:   {_400_TEXT}",
        *_tail(),
    ]


def test_exchange_failure_prefers_cause_envelope_over_detail_args() -> None:
    error = dict(_INCIDENT_ERROR)
    error["cause"] = {
        "type": "model.timeout",
        "message": "the model did not answer in 30s",
        "where": "llm.call",
        "detail": {},
        "cause": None,
    }

    lines = format_exchange_failure(
        _failed({"error": error}), agent_ref=_AGENT
    ).splitlines()

    assert lines[1] == "  cause:   model.timeout: the model did not answer in 30s"


def test_exchange_failure_long_multiline_cause_is_one_capped_line() -> None:
    error = dict(_INCIDENT_ERROR)
    error["detail"] = {"args": [("word " * 40 + "\n") * 10]}  # ~2,000 chars

    lines = format_exchange_failure(
        _failed({"error": error}), agent_ref=_AGENT
    ).splitlines()

    assert len(lines) == 4
    cause = lines[1].removeprefix("  cause:   ")
    assert len(cause) <= CAUSE_MAX_CHARS
    assert cause.endswith("…")
    assert "\n" not in cause
    assert "  " not in cause


def test_exchange_failure_without_any_cause_omits_the_line() -> None:
    error = {"type": "model.timeout", "message": "timed out", "detail": {}}

    lines = format_exchange_failure(
        _failed({"error": error}), agent_ref=_AGENT
    ).splitlines()

    assert lines == [f"error: {_AGENT} failed — model.timeout: timed out", *_tail()]


def test_exchange_failure_collapses_a_multiline_message_in_the_header() -> None:
    error = {"type": "x.y", "message": "line one\n  line two"}

    header = format_exchange_failure(
        _failed({"error": error}), agent_ref=_AGENT
    ).splitlines()[0]

    assert header == f"error: {_AGENT} failed — x.y: line one line two"


def test_exchange_failure_is_total_on_malformed_envelopes() -> None:
    fallback = [f"error: {_AGENT} failed (event {_EVENT_ID})", *_tail()]
    for data in (
        {},
        {"error": "not a dict"},
        {"error": None},
        {"error": {"message": "no type"}},
        {"error": {"type": 3, "message": "bad type"}},
    ):
        out = format_exchange_failure(_failed(data), agent_ref=_AGENT)
        assert out.splitlines() == fallback, data


def test_exchange_failure_tolerates_odd_cause_and_detail_shapes() -> None:
    extras: tuple[dict[str, Any], ...] = (
        {"cause": "a string", "detail": "not a dict"},
        {"cause": {}, "detail": {"args": []}},
        {"cause": None, "detail": {"args": "not a list"}},
        {"detail": {"args": ["   "]}},
    )
    for extra in extras:
        error = {"type": "x.y", "message": "m", **extra}
        lines = format_exchange_failure(
            _failed({"error": error}), agent_ref=_AGENT
        ).splitlines()
        assert lines == [f"error: {_AGENT} failed — x.y: m", *_tail()], extra


def test_internal_failure_names_type_and_points_at_v_and_log(tmp_path: Path) -> None:
    out = format_internal_failure(
        ValueError("boom\nsecond line"), agent_ref=_AGENT, workspace_root=tmp_path
    )

    assert out.splitlines() == [
        f"error: {_AGENT} failed with an internal error (ValueError): boom second line",
        "  re-run with `kolsch -v` for the traceback; full details in "
        f"{tmp_path / 'logs' / 'kolsch.log'}",
    ]


def test_internal_failure_empty_message(tmp_path: Path) -> None:
    out = format_internal_failure(
        RuntimeError(), agent_ref=_AGENT, workspace_root=tmp_path
    )
    assert "(RuntimeError): (no message)" in out.splitlines()[0]


def test_one_line_limit_counts_the_ellipsis() -> None:
    assert one_line("a\n  b\tc") == "a b c"
    assert one_line("x" * CAUSE_MAX_CHARS) == "x" * CAUSE_MAX_CHARS
    cut = one_line("x" * (CAUSE_MAX_CHARS + 1))
    assert len(cut) == CAUSE_MAX_CHARS
    assert cut.endswith("…")
