"""The `EventBus` Protocol — the public contract every backend honours.

v1 ships [`InProcessEventBus`][bus.in_process.InProcessEventBus]; a
Redis-backed backend slots in at the same Protocol surface when
cross-process delivery is needed. See the
[event bus contract](../explanation/event-bus-contract.md) for
the full semantics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from schemas import Event, SubscriptionId


# Subscriber handlers receive one event per invocation and return a
# coroutine. Handlers MUST be safe to call concurrently; failures are
# isolated by the bus so one handler cannot break siblings.
EventHandler = "Callable[[Event], Awaitable[None]]"


class EventBus(Protocol):
    """The bus contract — publish, subscribe, unsubscribe.

    All methods are async. Implementations dispatch via the configured
    backend (asyncio pub/sub in-process by default, Redis at scale).
    """

    async def publish(self, event: Event) -> None:
        """Publish an event and deliver it to every matching subscriber.

        Best-effort — a transport failure MUST NOT raise into the
        caller. Slow or failing subscribers do NOT block the publisher.

        Args:
            event: The event to fan out to matching subscribers.
        """

    async def subscribe(
        self,
        topic_pattern: str,
        handler: Callable[[Event], Awaitable[None]],
        filters: dict[str, object] | None = None,
    ) -> SubscriptionId:
        """Register `handler` for events matching `topic_pattern`.

        See `bus.patterns` for the pattern grammar. `filters` is
        AND-applied alongside the topic match.

        Args:
            topic_pattern: Full wildcard (`"*"`), prefix wildcard
                (`"tool.*"`), or exact event type.
            handler: Coroutine invoked once per matching event.
            filters: Optional equality checks AND-applied against
                event fields.

        Returns:
            A handle usable to unsubscribe later.
        """

    async def unsubscribe(self, subscription_id: SubscriptionId) -> None:
        """Remove the subscription. Idempotent; unknown handles are no-ops."""

    async def close(self) -> None:
        """Release backend resources. Idempotent."""
