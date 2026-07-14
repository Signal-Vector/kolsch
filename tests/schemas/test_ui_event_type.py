"""spec-002 Test 1 — the `UI_EVENT` `EventType` member.

Pins:

- The enum value is the canonical wire string ``"ui.event"``.
- Round-trip: ``EventType("ui.event")`` returns the enum member.
- The addition is purely additive — every pre-existing member is
  still present.
"""

from __future__ import annotations

from schemas import EventType


def test_ui_event_value_is_ui_dot_event() -> None:
    """``EventType.UI_EVENT.value`` is the canonical wire string."""
    assert EventType.UI_EVENT.value == "ui.event"


def test_ui_event_round_trips_from_string() -> None:
    """Constructing the enum from its string value returns the member."""
    assert EventType("ui.event") is EventType.UI_EVENT


def test_ui_event_addition_is_purely_additive() -> None:
    """spec-002 only ADDS to the enum — no pre-existing member is
    removed. This test enumerates the pre-spec-002 members and asserts
    each is still present alongside ``UI_EVENT``."""
    pre_spec_002_members = {
        "SESSION_OPENED",
        "THREAD_EXCHANGE_STARTED",
        "THREAD_EXCHANGE_COMPLETED",
        "THREAD_EXCHANGE_FAILED",
        "USER_MESSAGE",
        "AGENT_MESSAGE",
        "TOOL_CALLED",
        "TOOL_RESULT",
        "TOOL_FAILED",
        "MODEL_REQUEST",
        "MODEL_RESPONSE",
        "MODEL_STREAMING_TOKEN",
        "MODEL_FALLBACK",
        "CONFIG_SET",
        "MEMORY_WRITTEN",
        "MEMORY_RETRIEVED",
        "MEMORY_DELETED",
        "CONTEXT_COMPACTED",
        "AGENT_TEMPLATE_WARNING",
    }
    member_names = {m.name for m in EventType}
    missing = pre_spec_002_members - member_names
    assert not missing, f"pre-spec-002 members removed: {missing}"
    assert "UI_EVENT" in member_names
