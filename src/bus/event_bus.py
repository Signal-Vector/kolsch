"""EventBus Protocol — Module API Reference §4.

The public contract every bus implementation honours. v1 ships
:class:`bus.InProcessEventBus`; a Redis-backed implementation slots in
at the same Protocol surface when cross-process delivery (Tier 2+)
is needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from schemas import Event, SubscriptionId


# Subscriber handlers receive one event per invocation and return a
# coroutine. Per spec §4.3 the handler MUST be safe to call
# concurrently; failures are caught by the bus (§I4).
EventHandler = "Callable[[Event], Awaitable[None]]"


class EventBus(Protocol):
    """The bus contract — publish / subscribe / unsubscribe.

    All methods are async (spec §I8). Implementations dispatch via
    the configured backend (asyncio pub/sub in-process by default,
    Redis at scale).
    """

    async def publish(self, event: Event) -> None:
        """Publish an event; deliver to every matching subscriber.

        Best-effort per spec §I5/§I7 — a transport failure MUST NOT
        raise into the caller. Slow or failing subscribers do NOT
        block the publisher.
        """

    async def subscribe(
        self,
        topic_pattern: str,
        handler: Callable[[Event], Awaitable[None]],
        filters: dict[str, object] | None = None,
    ) -> SubscriptionId:
        """Register ``handler`` for events matching ``topic_pattern``.

        See :mod:`bus.patterns` for pattern grammar. ``filters`` is
        AND-applied alongside the topic match. Returns a
        :class:`SubscriptionId` usable to unsubscribe.
        """

    async def unsubscribe(self, subscription_id: SubscriptionId) -> None:
        """Remove the subscription. Idempotent; unknown handles are no-ops."""

    async def close(self) -> None:
        """Release backend resources. Idempotent."""
