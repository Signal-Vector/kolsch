"""Bus — the ephemeral event bus (in-process default, Redis scale-up).

Implements the Bus subsystem per the
[event bus contract](../explanation/event-bus-contract.md).
Delivery is best-effort; publish failures never raise into the engine's
execution path. Durability for strict subscribers comes from the log
(Session/Run Store), not from the bus.
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
