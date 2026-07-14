"""spec-068 Part B Test 4 — renderer performs no durable-repair writes.

The engine now owns dangling-span repair (via
``_Exchange._reconcile_open_spans_on_resume``); the renderer's
former ``_scan_dangling_prompts`` + ``_append_synthetic_*`` helpers
are gone. This test pins the invariant so a future refactor can't
reintroduce a renderer-side durable repair write:

- Source-level: ``EventAdapter`` no longer defines the removed
  helpers; the only remaining ``append_event`` call in the module is
  the pre-existing ``WIRE_PROMPT_ANSWERED`` write from the approval
  bridge (a normal terminal on user-answer, not a repair).
- Runtime: ``stage_replay_prompt_outcomes`` still marks dangling
  prompts ``⊘ Interrupted`` IN-MEMORY (the ``_replay_prompt_outcomes``
  map) and does not touch the store.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from cli.chat.event_adapter import EventAdapter
from schemas import (
    Actor,
    Event,
    EventType,
    SessionMeta,
)
from schemas.identifiers import EventId, SessionId, ThreadId
from schemas.wire_kinds import (
    WIRE_PROMPT_REQUESTED,
)

_TEST_SESSION_ID = SessionId(f"ses_{uuid4()}")
_TEST_THREAD_ID = ThreadId(f"th_{uuid4()}")


# ---------------------------------------------------------------------------
# Minimal ChatApp double — required by EventAdapter constructor.
# ---------------------------------------------------------------------------


class _MockChatApp:
    def __init__(self) -> None:
        self._last_assistant: str = ""

    async def mount_user_message(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def mount_assistant_message(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def mount_assistant_streaming_widget(self) -> Any:
        class _MockProse:
            async def append(self, text: str) -> None:
                return None

        return _MockProse()

    async def mount_tool_call(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def mount_notice(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def mount_exchange_failed(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def set_turn_verb(self, *args: Any, **kwargs: Any) -> None:
        return None

    def clear_awaiting_approval(self) -> None:
        return None

    def last_assistant_text(self) -> str:
        return self._last_assistant

    def set_last_assistant_text(self, text: str) -> None:
        self._last_assistant = text


# ---------------------------------------------------------------------------
# Test 1 — source-level: no session-interrupted repair writes remain.
# ---------------------------------------------------------------------------


def test_event_adapter_source_has_no_repair_helpers() -> None:
    """spec-068 Part B — the removed methods must stay removed.

    The renderer used to synth `.cancelled` + `tool.failed` from
    `_append_synthetic_prompt_cancelled` / `_append_synthetic_tool_
    failed`, called by `_scan_dangling_prompts` (an I1 violation
    AND tail-windowed at 1000 events — the exact reason the
    permanent-wedge bug was possible in long sessions). All three
    are gone; source-level grep is the regression backstop.
    """
    source_path = Path(__file__).parents[4] / "src/cli/chat/event_adapter.py"
    src = source_path.read_text(encoding="utf-8")
    for banned in (
        "_scan_dangling_prompts",
        "_append_synthetic_prompt_cancelled",
        "_append_synthetic_tool_failed",
    ):
        assert f"async def {banned}" not in src, (
            f"{banned} must be deleted; it was the renderer-side I1 "
            f"violation spec-068 Part B fixes."
        )


def test_event_adapter_has_no_session_interrupted_writes() -> None:
    """Any `append_event` call that follows a `session.interrupted`
    payload build inside `EventAdapter` is a repair write and violates
    the sole-emitter invariant. The engine owns those writes now.
    """
    source_path = Path(__file__).parents[4] / "src/cli/chat/event_adapter.py"
    src = source_path.read_text(encoding="utf-8")
    # A "session.interrupted" or "session_interrupted" string in
    # event_adapter.py would mean a repair terminal payload survived
    # the deletion. The only allowed occurrences are docstrings /
    # comments explaining the migration (checked separately by
    # confirming no `append_event` follows within the SAME method
    # body). We take the strict form: no such string may appear.
    assert "session.interrupted" not in src, (
        "session.interrupted string in event_adapter.py suggests a "
        "repair payload was reintroduced; the engine owns these now."
    )
    assert "session_interrupted" not in src, (
        "session_interrupted string in event_adapter.py suggests a "
        "repair payload was reintroduced; the engine owns these now."
    )


def test_event_adapter_remaining_append_event_calls_are_not_repair() -> None:
    """The only allowed `append_event` call in event_adapter.py is
    the approval-bridge `WIRE_PROMPT_ANSWERED` write — a normal
    user-answered terminal, not a repair. Any other call site is a
    regression against spec-068 Part B.
    """
    source_path = Path(__file__).parents[4] / "src/cli/chat/event_adapter.py"
    src = source_path.read_text(encoding="utf-8")
    matches = list(re.finditer(r"append_event\s*\(", src))
    # Confirm each surviving append_event site is contextually a
    # WIRE_PROMPT_ANSWERED (user-answered terminal from the approval
    # bridge) — walk back ~500 chars from each match and confirm the
    # payload kind is WIRE_PROMPT_ANSWERED. Anything else is a repair
    # and must fail the test.
    for match in matches:
        start = max(0, match.start() - 800)
        surrounding = src[start : match.end()]
        assert "WIRE_PROMPT_ANSWERED" in surrounding, (
            f"append_event call at src[{match.start()}] does not "
            f"appear to be the approval-bridge WIRE_PROMPT_ANSWERED "
            f"terminal — spec-068 Part B forbids repair writes. "
            f"Surrounding context: {surrounding[-400:]!r}"
        )


# ---------------------------------------------------------------------------
# Test 2 — runtime: `stage_replay_prompt_outcomes` writes nothing durable.
# ---------------------------------------------------------------------------


class _RecordingSessionStore:
    """SessionStore stub that records any `append_event` call.

    If `stage_replay_prompt_outcomes` regresses into writing to the
    store, this stub sees the write and the assertion at the end of
    the test fails with the exact payload.
    """

    def __init__(self) -> None:
        self.append_calls: list[Event] = []

    async def append_event(self, thread_id: Any, event: Event) -> Event:
        self.append_calls.append(event)
        return event


def _mk_ui_event(
    *,
    kind: str,
    prompt_id: str,
    payload_extras: dict[str, Any] | None = None,
) -> Event:
    payload: dict[str, Any] = {"prompt_id": prompt_id}
    if payload_extras:
        payload.update(payload_extras)
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=_TEST_THREAD_ID,
        session_id=_TEST_SESSION_ID,
        type=EventType.UI_EVENT,
        data={
            "schema_version": 1,
            "kind": kind,
            "payload": payload,
        },
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


def test_stage_replay_prompt_outcomes_marks_dangling_in_memory_only() -> None:
    """spec-068 Part B — the render-only path stays intact.

    Feed a dangling `.requested` (no matching `.answered` /
    `.cancelled` in-slice); assert the outcome map records
    `("interrupted", "⊘ Interrupted")` for the prompt AND the
    recording store spy never receives an `append_event` call.
    """
    store = _RecordingSessionStore()
    adapter = EventAdapter(
        _MockChatApp(),
        session_store=store,
        session_id=_TEST_SESSION_ID,
        thread_id=_TEST_THREAD_ID,
        session_meta=SessionMeta(),
    )
    dangling = _mk_ui_event(
        kind=WIRE_PROMPT_REQUESTED,
        prompt_id="prm_dangling",
        payload_extras={"kind": "choice", "prompt": "Allow?"},
    )
    adapter.stage_replay_prompt_outcomes([dangling])

    # In-memory outcome recorded — the row will render as ⊘ Interrupted.
    outcome = adapter._replay_prompt_outcomes.get("prm_dangling")
    assert outcome == ("interrupted", "⊘ Interrupted"), (
        f"expected interrupted outcome; got {outcome!r}"
    )

    # Store never touched.
    assert store.append_calls == [], (
        f"stage_replay_prompt_outcomes must write nothing durable; "
        f"got {len(store.append_calls)} appends: "
        f"{[e.data for e in store.append_calls]}"
    )


def test_stage_replay_prompt_outcomes_does_not_touch_composer_freeze() -> None:
    """The render-side path relies on `recompute_awaiting_approval`
    (called by `_replay_history` AFTER the dispatch loop) to unfreeze
    the composer. `stage_replay_prompt_outcomes` itself is a pure
    pre-pass — it must not attempt to freeze or unfreeze anything.
    """
    store = _RecordingSessionStore()

    # A ChatApp double that raises if any freeze/unfreeze method is
    # called — the pre-pass has no business touching UI state.
    class _StrictApp(_MockChatApp):
        def clear_awaiting_approval(self) -> None:
            msg = "stage_replay_prompt_outcomes must not touch composer freeze state"
            raise AssertionError(msg)

    adapter = EventAdapter(
        _StrictApp(),
        session_store=store,
        session_id=_TEST_SESSION_ID,
        thread_id=_TEST_THREAD_ID,
        session_meta=SessionMeta(),
    )
    adapter.stage_replay_prompt_outcomes(
        [
            _mk_ui_event(
                kind=WIRE_PROMPT_REQUESTED,
                prompt_id="prm_1",
                payload_extras={"kind": "choice"},
            ),
        ]
    )
    # If we got here without raising, the pre-pass didn't touch
    # composer state — invariant preserved.
