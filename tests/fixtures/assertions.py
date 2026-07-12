"""Event-sequence assertion helpers — per handbook §8.6.

Three helpers that read clearly in tests instead of drowning in dict
comparisons:

- ``assert_event_sequence`` — the right event types in the right order.
- ``assert_event_content`` — an event's data matches the expected fields.
- ``assert_materialization`` — what the Mock LLM was asked to process
  equals what the events captured (the Materialization Rule check from
  Schemas §B.6).

Events are typed loosely (``dict[str, Any]``) until the canonical
``Event`` model ships with the store. Helpers accept that shape today
and the typed model later without changing call sites.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tests.fixtures.mock_llm import MockLLM


def assert_event_sequence(
    events: Sequence[dict[str, Any]],
    expected_types: Sequence[str],
) -> None:
    """Assert that ``events`` carry ``expected_types`` in that exact order.

    A mismatch produces an actionable diff so reviewers see precisely
    which event drifted.
    """
    actual_types = [event["type"] for event in events]
    if list(actual_types) != list(expected_types):
        msg = (
            "event sequence mismatch\n"
            f"  expected: {list(expected_types)}\n"
            f"  actual:   {actual_types}"
        )
        raise AssertionError(msg)


def assert_event_content(event: dict[str, Any], **expected: Any) -> None:
    """Assert that ``event['data']`` contains the given key/value pairs.

    Extra keys in ``event['data']`` are ignored; the helper checks that
    each expected key is present and equal. This keeps tests focused on
    what they care about without being brittle to additive event data.
    """
    data = event.get("data", {})
    missing = [key for key in expected if key not in data]
    if missing:
        msg = (
            "event missing expected data keys: "
            f"{missing} (event type={event.get('type')!r})"
        )
        raise AssertionError(msg)
    mismatched = [
        (key, data[key], wanted)
        for key, wanted in expected.items()
        if data[key] != wanted
    ]
    if mismatched:
        details = ", ".join(
            f"{key}: expected {wanted!r}, got {actual!r}"
            for key, actual, wanted in mismatched
        )
        msg = f"event data mismatch (type={event.get('type')!r}): {details}"
        raise AssertionError(msg)


def assert_materialization(
    events: Sequence[dict[str, Any]],
    mock_llm: MockLLM,
) -> None:
    """Assert the model saw what the events captured.

    The Materialization Rule (Schemas §B.6): every input that
    contributed to a model call's context is captured in an immutable
    event. This helper compares the context the Mock LLM recorded
    against what the events on the path say should have been there.

    Stub: the full Assembly Procedure (Schemas §B.5) lives in the
    engine. Once it's published, this helper invokes the procedure on
    `events` at the assembly_version recorded on `config.set` and
    compares byte-for-byte against each `mock_llm.calls[i].context`.

    For now: assert the engine actually drove the Mock LLM at least
    once per `model.request` event. This is the weakest non-trivial
    check; it catches "wired up wrong / engine never called the model"
    and will tighten as the procedure lands.
    """
    request_events = [event for event in events if event.get("type") == "model.request"]
    if len(request_events) != len(mock_llm.calls):
        msg = (
            "materialization mismatch: "
            f"{len(request_events)} model.request event(s) but "
            f"{len(mock_llm.calls)} recorded Mock LLM call(s). "
            "Each model.request must correspond to one model client call."
        )
        raise AssertionError(msg)
