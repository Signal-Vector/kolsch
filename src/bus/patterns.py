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

    Empty / None filters match everything. Comparison is plain equality
    against the event's field of that name; an unknown field is treated
    as "no match" (the event lacks the attribute the caller asked to
    filter on).
    """
    if not filters:
        return True
    for key, expected in filters.items():
        actual = getattr(event, key, None)
        if actual != expected:
            return False
    return True
