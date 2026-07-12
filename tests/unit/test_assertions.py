"""Unit tests for the event-sequence assertion helpers.

Per handbook §8.6. Each helper has both a pass path and a fail path
exercised here so a future refactor that breaks one path lights up.
"""

import pytest

from tests.fixtures.assertions import (
    assert_event_content,
    assert_event_sequence,
    assert_materialization,
)
from tests.fixtures.factories import make_event
from tests.fixtures.mock_llm import MockLLM, TextResponse

# --- assert_event_sequence -------------------------------------------------


def test_event_sequence_matches() -> None:
    events = [
        make_event("user.message"),
        make_event("model.request"),
        make_event("model.response"),
    ]

    assert_event_sequence(
        events,
        ["user.message", "model.request", "model.response"],
    )


def test_event_sequence_mismatch_raises_with_diff() -> None:
    events = [make_event("user.message"), make_event("tool.called")]

    with pytest.raises(AssertionError) as exc_info:
        assert_event_sequence(events, ["user.message", "model.request"])

    msg = str(exc_info.value)
    assert "expected" in msg
    assert "actual" in msg
    assert "tool.called" in msg


def test_event_sequence_extra_event_raises() -> None:
    events = [make_event("user.message"), make_event("model.request")]

    with pytest.raises(AssertionError):
        assert_event_sequence(events, ["user.message"])


# --- assert_event_content --------------------------------------------------


def test_event_content_matches() -> None:
    event = make_event("tool.called", tool_name="read", tool_call_id="t1")

    assert_event_content(event, tool_name="read", tool_call_id="t1")


def test_event_content_ignores_extra_data_keys() -> None:
    event = make_event(
        "tool.called",
        tool_name="read",
        arguments={"path": "x"},
        tool_call_id="t1",
    )

    # The helper only checks the keys the caller cares about; additive
    # data is intentionally not brittle.
    assert_event_content(event, tool_name="read")


def test_event_content_missing_key_raises() -> None:
    event = make_event("tool.called", tool_name="read")

    with pytest.raises(AssertionError) as exc_info:
        assert_event_content(event, tool_name="read", tool_call_id="t1")

    assert "tool_call_id" in str(exc_info.value)


def test_event_content_value_mismatch_raises() -> None:
    event = make_event("tool.called", tool_name="read")

    with pytest.raises(AssertionError) as exc_info:
        assert_event_content(event, tool_name="write")

    msg = str(exc_info.value)
    # Make sure both the expected and the actual show up — that's the
    # whole point of the helper over a raw `assert ==`.
    assert "'write'" in msg
    assert "'read'" in msg


# --- assert_materialization ------------------------------------------------


async def test_materialization_matches_count() -> None:
    mock = MockLLM(script=[TextResponse(text="hi"), TextResponse(text="bye")])
    await mock.agenerate({"turn": 1})
    await mock.agenerate({"turn": 2})

    events = [
        make_event("model.request"),
        make_event("agent.message"),
        make_event("model.request"),
    ]

    assert_materialization(events, mock)


async def test_materialization_count_mismatch_raises() -> None:
    mock = MockLLM(script=[TextResponse(text="hi")])
    await mock.agenerate({})

    events = [make_event("model.request"), make_event("model.request")]

    with pytest.raises(AssertionError) as exc_info:
        assert_materialization(events, mock)

    assert "materialization mismatch" in str(exc_info.value)


def test_materialization_zero_calls_zero_requests_ok() -> None:
    # Edge case: no requests, no calls. Trivially consistent.
    assert_materialization([make_event("session.opened")], MockLLM())
