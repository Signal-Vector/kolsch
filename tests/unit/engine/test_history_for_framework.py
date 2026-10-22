"""bug-156 — ``engine.engine._history_for_framework``.

The engine's framework ``history`` is everything before the last user
message, plus any compaction summary projected AFTER it (the
per-exchange pre-flight emits ``context.compacted`` after the trigger
``user.message``), appended immediately before the user message.

- No compaction message → identical to ``_history_before_last_user``.
- Trailing compaction messages → appended in order at the end.
- A compaction message before the last user message stays in place and
  is not duplicated or moved.
- A non-compaction message after the last user message is still dropped.
- No user message → ``[]``.
"""

from __future__ import annotations

from engine.assembly import COMPACTION_MESSAGE_TAG, is_compaction_summary_message
from engine.engine import _history_before_last_user, _history_for_framework
from engine.framework import Message


def _summary(text: str) -> Message:
    return Message(
        role="system",
        content=f"{COMPACTION_MESSAGE_TAG}\n{text}\n</context-compaction>",
    )


def test_no_compaction_message_matches_history_before_last_user() -> None:
    messages = [
        Message(role="user", content="q1"),
        Message(role="assistant", content="a1"),
        Message(role="user", content="q2"),
    ]
    assert _history_for_framework(messages) == _history_before_last_user(messages)
    assert _history_for_framework(messages) == messages[:2]


def test_trailing_compaction_messages_are_appended_in_order() -> None:
    first, second = _summary("one"), _summary("two")
    messages = [
        Message(role="user", content="q1"),
        Message(role="assistant", content="a1"),
        Message(role="user", content="q2"),
        first,
        second,
    ]
    assert _history_for_framework(messages) == [messages[0], messages[1], first, second]


def test_compaction_message_before_last_user_stays_in_place() -> None:
    older = _summary("older")
    newer = _summary("newer")
    messages = [
        Message(role="user", content="q1"),
        older,
        Message(role="assistant", content="a1"),
        Message(role="user", content="q2"),
        newer,
    ]
    history = _history_for_framework(messages)
    assert history == [messages[0], older, messages[2], newer]
    assert history.count(older) == 1


def test_non_compaction_message_after_last_user_is_dropped() -> None:
    messages = [
        Message(role="user", content="q1"),
        Message(role="assistant", content="a1"),
        Message(role="user", content="q2"),
        Message(role="tool", content="late tool result", tool_call_id="t1"),
        Message(role="system", content="<memory>\nnot a summary\n</memory>"),
        Message(role="assistant", content=f"{COMPACTION_MESSAGE_TAG} quoted"),
    ]
    assert _history_for_framework(messages) == messages[:2]


def test_no_user_message_gives_empty_history() -> None:
    messages = [_summary("orphan"), Message(role="assistant", content="a")]
    assert _history_for_framework(messages) == []
    assert _history_for_framework([]) == []


def test_is_compaction_summary_message_requires_system_role_and_prefix() -> None:
    assert is_compaction_summary_message(_summary("s"))
    assert not is_compaction_summary_message(
        Message(role="user", content=f"{COMPACTION_MESSAGE_TAG} typed by a user")
    )
    assert not is_compaction_summary_message(
        Message(role="system", content="You are a test agent.")
    )
    assert not is_compaction_summary_message(Message(role="system", content=""))
