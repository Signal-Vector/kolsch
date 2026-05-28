"""Bus — ephemeral event bus (in-process default, Redis scale-up).

Per Module API Reference §4 + docs/explanation/event-bus-contract.md.
Best-effort delivery; publish failures never raise into the engine's
execution path. Strict subscribers durability via the log (Session/
Run Store), not via the bus.

Public surface:

- :class:`EventBus` — the Protocol every backend honours.
- :class:`InProcessEventBus` — the v1 default implementation.
- Pattern helpers :func:`topic_matches` and
  :func:`event_matches_filters` are re-exported so callers can reason
  about subscription patterns without importing the private module.
"""

from bus.event_bus import EventBus
from bus.in_process import InProcessEventBus
from bus.patterns import event_matches_filters, topic_matches

__all__ = [
    "EventBus",
    "InProcessEventBus",
    "event_matches_filters",
    "topic_matches",
]
