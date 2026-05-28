"""Topic + attribute-filter matching — spec §3.

Three topic-pattern shapes:

- ``"*"`` — full wildcard, matches every event.
- ``"<prefix>*"`` — prefix wildcard. The prefix is everything before
  the trailing ``*`` and matches as a literal string. Spec §3.1
  examples show both ``"tool.*"`` (prefix ends at the dot) and
  ``"thread.exchange_*"`` (prefix ends mid-segment) so the matcher
  takes any literal prefix, not only dotted ones. Embedded ``*``
  inside the pattern is treated as a literal character; since real
  event types never contain ``*`` this degrades cleanly to "no match".
- ``"tool.failed"`` — exact match on ``event.type``.

Attribute filters are AND-combined key/value equality checks against
:class:`schemas.Event` fields. The matcher is field-name-agnostic so
future fields work without changes here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from schemas import Event

_FULL_WILDCARD = "*"


def topic_matches(pattern: str, event_type: str) -> bool:
    """Return True when ``event_type`` satisfies ``pattern``.

    The three shapes (full wildcard, prefix wildcard, exact) are
    exhaustive; an empty pattern is treated as exact (and so never
    matches a non-empty event type).
    """
    if pattern == _FULL_WILDCARD:
        return True
    if pattern.endswith(_FULL_WILDCARD):
        prefix = pattern[:-1]
        return event_type.startswith(prefix)
    return pattern == event_type


def event_matches_filters(event: Event, filters: dict[str, object]) -> bool:
    """Return True when ``event`` satisfies every (key, value) filter.

    Empty / ``None`` filters match everything. Comparison is plain
    equality against the event's field of that name.

    Unknown filter keys are treated as "no match" — a typo in a filter
    key (e.g. ``{"thread_idd": th_a}``) would otherwise silently
    broaden delivery to every event, which is the opposite of the
    caller's intent. Subscribers asking to filter on a field the
    event does not carry get nothing.
    """
    if not filters:
        return True
    for key, expected in filters.items():
        if not hasattr(event, key):
            return False
        if getattr(event, key) != expected:
            return False
    return True
