"""Shared fixtures for chat/widget Pilot tests.

Widget agents assume:

- `mock_app_factory` — returns a callable that wraps ONE widget in a
  minimal `App` for `App.run_test()`. Used for widget-in-isolation tests.
- `canonical_event` — builds a well-formed `Event` for a given
  `EventType`. Used by `test_event_adapter_coverage.py` to drive every
  branch of `EventAdapter.handle` with a real payload.

Both fixtures are cheap, framework-native, and scoped to `function`
so tests don't share state.

Widget subsections of `process/spec-textual-chat-ui.md` are the
source of truth for what each widget expects at mount time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from textual.app import App, ComposeResult

from engine.engine import (
    _current_execution_context,
    current_execution_context,
)
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.widget import Widget


@pytest.fixture(autouse=True)
def _isolate_current_execution_context() -> Any:
    """Guarantee `_current_execution_context` starts at `None` per test.

    bug-107 wired `_derive_source` to fall back to this ContextVar when
    its parameter is `None`. Existing tests
    (`test_approval_callback_refactor.py::test_derive_source_workflow_scoped`
    and peers) pass `execution_context=None` and expect
    `source["kind"] == "tool"`; if a prior test on the same worker leaked
    a workflow-scoped `ExecutionContext` into the ContextVar, that
    expectation would silently flip. This fixture asserts the pristine
    state on entry, captures a `Token` for the (usually already-None)
    current value, and resets after the test so any test that sets the
    var directly is cleaned up.
    """
    assert current_execution_context() is None, (
        "test-suite bleed: _current_execution_context should be None on "
        "setup but was populated — a prior test leaked its ExecutionContext"
    )
    token = _current_execution_context.set(None)
    try:
        yield
    finally:
        _current_execution_context.reset(token)


@pytest.fixture
def mock_app_factory() -> Callable[[Widget], App[None]]:
    """Return a factory that wraps a single widget in a minimal `App`.

    Usage in a widget test:

        async def test_something(mock_app_factory):
            widget = MyWidget(...)
            app = mock_app_factory(widget)
            async with app.run_test() as pilot:
                await pilot.press("y")
                # assert widget state / posted messages / rendered output

    The wrapper does not add CSS beyond what the widget declares in its
    own `DEFAULT_CSS`. This keeps tests honest — a widget that only
    renders correctly with theme.tcss loaded is a widget with hidden
    coupling, not a modular unit.
    """

    def _factory(widget: Widget) -> App[None]:
        class _WidgetHostApp(App[None]):
            def compose(self) -> ComposeResult:
                yield widget

        return _WidgetHostApp()

    return _factory


def _now() -> datetime:
    """Deterministic-enough timestamp for test payloads."""
    # NOTE: not literally `datetime.now()` because that varies per run;
    # tests that need a stable timestamp pass their own.
    return datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)


def _canonical_data(event_type: EventType) -> dict[str, Any]:
    """Return a minimal well-formed `data` dict for the given event.

    Extend this as new `EventType` members land. The
    `test_event_adapter_coverage.py` test parametrises over
    `list(EventType)` and calls this helper for each; missing branches
    surface as `KeyError` (fail loud, not silent).
    """
    match event_type:
        case EventType.SESSION_OPENED:
            return {"session_id": "sess-test"}
        case EventType.THREAD_EXCHANGE_STARTED:
            return {"thread_id": "thread-test"}
        case EventType.THREAD_EXCHANGE_COMPLETED:
            return {"thread_id": "thread-test"}
        case EventType.THREAD_EXCHANGE_FAILED:
            return {
                "thread_id": "thread-test",
                "error": {"type": "test", "message": "canonical"},
            }
        case EventType.USER_MESSAGE:
            return {"content": {"type": "text", "text": "hello"}}
        case EventType.AGENT_MESSAGE:
            return {"content": {"type": "text", "text": "hi back"}}
        case EventType.TOOL_CALLED:
            return {
                "tool_call_id": "call-1",
                "tool_name": "read_file",
                "arguments": {"path": "/tmp/x"},
            }
        case EventType.TOOL_RESULT:
            return {
                "tool_call_id": "call-1",
                "tool_name": "read_file",
                "output": "canonical",
            }
        case EventType.TOOL_FAILED:
            return {
                "tool_call_id": "call-1",
                "tool_name": "read_file",
                "error": {"type": "authz.denied", "message": "not allowed"},
            }
        case EventType.MODEL_REQUEST:
            return {"call_id": "call-1", "model": {}, "messages": [], "tools": []}
        case EventType.MODEL_RESPONSE:
            return {
                "call_id": "call-1",
                "content": {"type": "text", "text": "response"},
                "usage": {},
            }
        case EventType.MODEL_STREAMING_TOKEN:
            return {"call_id": "call-1", "text_delta": "chunk"}
        case EventType.MODEL_FALLBACK:
            return {"from_model": "m1", "to_model": "m2", "reason": "canonical"}
        case EventType.CONFIG_SET:
            return {"key": "k", "value": "v"}
        case EventType.MEMORY_WRITTEN:
            return {"key": "k", "value": "v"}
        case EventType.MEMORY_RETRIEVED:
            return {"key": "k", "value": "v"}
        case EventType.MEMORY_DELETED:
            return {"key": "k"}
        case EventType.CONTEXT_COMPACTED:
            return {"before_tokens": 100, "after_tokens": 50}
        case EventType.CONTEXT_COMPACTION_REVOKED:
            return {"compaction_event_id": f"ev_{uuid4()}", "reason": "canonical"}
        case EventType.CONTEXT_COMPACTION_FAILED:
            return {
                "error": {
                    "type": "context.summarizer_failed",
                    "message": "canonical",
                    "where": "providers.compaction",
                    "detail": {},
                }
            }
        case EventType.CONTEXT_COMPACTION_SKIPPED:
            return {"reason": "canonical", "tokens_before": 100, "tokens_after": 100}
        case EventType.AGENT_TEMPLATE_WARNING:
            return {"message": "canonical warning"}
        case EventType.UI_EVENT:
            # spec-002 envelope: dotted `kind` + free-shape `payload`.
            # No emitter ships yet (spec-003+); adapter handles it as a
            # silent no-op so replay of a future JSONL row doesn't raise.
            return {
                "schema_version": 1,
                "kind": "kolsch.card.opened",
                "payload": {"card_id": "canonical"},
            }
    # Fail loud on any missing branch; the coverage test relies on this.
    msg = f"conftest.canonical_event: no branch for {event_type!r}"  # type: ignore[unreachable]  # defensive fallback for future EventType members not yet in the exhaustive match
    raise KeyError(msg)


@pytest.fixture
def canonical_event() -> Callable[[EventType], Event]:
    """Return a factory that builds a canonical `Event` for a given type.

    Used by `test_event_adapter_coverage.py` to drive every branch of
    `EventAdapter.handle` with a real payload shape. If a new
    `EventType` lands upstream, add the branch in `_canonical_data`
    and the coverage test picks it up automatically via
    `list(EventType)`.
    """

    def _factory(event_type: EventType, **overrides: Any) -> Event:
        data = _canonical_data(event_type) | overrides
        return Event(
            event_id=EventId(f"ev_{uuid4()}"),
            thread_id=ThreadId(f"th_{uuid4()}"),
            session_id=SessionId(f"ses_{uuid4()}"),
            type=event_type,
            data=data,
            actor=Actor(kind="system"),
            created_at=_now(),
        )

    return _factory
