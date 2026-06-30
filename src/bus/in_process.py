"""In-process [`EventBus`][bus.event_bus.EventBus] backend — the v1 default.

Zero external services. Subscriptions are held in an in-memory dict;
publish dispatches to every matching handler via `asyncio.gather` with
`return_exceptions=True` so a raising handler cannot block siblings.

The whole dispatch path is wrapped in a try/except so that a transport
failure (anything below the handler-level isolation) is logged and
swallowed — the publisher's primary work is never broken by a bus
failure. See the
[event bus contract](../../docs/explanation/event-bus-contract.md) for
the full delivery semantics.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from bus.patterns import event_matches_filters, topic_matches
from schemas import SubscriptionId

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from schemas import Event


_log = logging.getLogger("bus")


@dataclass(frozen=True)
class _Subscription:
    """One in-memory subscription record."""

    subscription_id: SubscriptionId
    topic_pattern: str
    handler: Callable[[Event], Awaitable[None]]
    filters: dict[str, object]


class InProcessEventBus:
    """In-process pub/sub — the v1 default backend.

    Conforms to [`EventBus`][bus.event_bus.EventBus] (structural
    typing). The constructor takes no arguments; the bus is
    self-contained.
    """

    def __init__(self) -> None:
        self._subscriptions: dict[SubscriptionId, _Subscription] = {}
        # Mutations (subscribe/unsubscribe) and reads (publish's
        # snapshot of the subscriber set) serialised through this lock
        # so reasoning about concurrency is local.
        self._lock = asyncio.Lock()
        self._closed = False

    async def publish(self, event: Event) -> None:
        if self._closed:
            return  # treat publish-after-close as no-op, not an error.
        try:
            # Take a snapshot of matching subscriptions so a concurrent
            # subscribe / unsubscribe doesn't mutate the set during
            # delivery.
            async with self._lock:
                matching = [
                    sub
                    for sub in self._subscriptions.values()
                    if topic_matches(sub.topic_pattern, event.type)
                    and event_matches_filters(event, sub.filters)
                ]
            await self._dispatch(event, matching)
        except Exception as exc:  # noqa: BLE001 — best-effort delivery
            # A transport failure MUST NOT raise into the caller. The
            # publisher's primary work — typically a log append that
            # already succeeded — is never broken by a bus failure.
            _log.warning("bus publish failed: %s", exc)

    async def _dispatch(
        self,
        event: Event,
        subscriptions: list[_Subscription],
    ) -> None:
        """Invoke every subscriber; isolate per-handler failures.

        Separated from `publish` so the publish-level try/except in
        `publish` is the single seam where a broken transport
        surfaces. Tests substitute this method to exercise the
        publish-failure-containment path.
        """
        if not subscriptions:
            return
        results = await asyncio.gather(
            *(sub.handler(event) for sub in subscriptions),
            return_exceptions=True,
        )
        for sub, result in zip(subscriptions, results, strict=True):
            if isinstance(result, BaseException):
                # Log the handler error and continue. The subscriber
                # stays registered; no redelivery, no dead-letter, no
                # auto-removal.
                _log.warning(
                    "subscriber %s handler raised: %s",
                    sub.subscription_id,
                    result,
                )

    async def subscribe(
        self,
        topic_pattern: str,
        handler: Callable[[Event], Awaitable[None]],
        filters: dict[str, object] | None = None,
    ) -> SubscriptionId:
        if self._closed:
            msg = "EventBus is closed"
            raise RuntimeError(msg)
        sub_id = SubscriptionId(str(uuid4()))
        record = _Subscription(
            subscription_id=sub_id,
            topic_pattern=topic_pattern,
            handler=handler,
            filters=dict(filters or {}),
        )
        async with self._lock:
            self._subscriptions[sub_id] = record
        return sub_id

    async def unsubscribe(self, subscription_id: SubscriptionId) -> None:
        # Idempotent; unknown handles are silently ignored rather than
        # raising.
        async with self._lock:
            self._subscriptions.pop(subscription_id, None)

    async def close(self) -> None:
        # Drop all subscriptions and mark closed; subsequent publish
        # calls become no-ops, subsequent subscribe calls raise.
        async with self._lock:
            self._subscriptions.clear()
            self._closed = True
