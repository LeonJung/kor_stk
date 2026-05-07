"""In-memory pub/sub event bus.

Topic = event class. Subscribers receive events whose runtime type is the
requested class or a subclass (isinstance dispatch). Each subscription
has a bounded queue; on overflow the oldest event is dropped so the
newest data wins — live trading favors freshness over completeness.

Assumes a single asyncio event loop. No additional thread safety
beyond what asyncio.Queue itself provides.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

log = logging.getLogger("ks_ws.bus")

_DEFAULT_MAXSIZE = 1024


class Subscription[T]:
    """A bounded async queue subscribed to one event type."""

    def __init__(self, event_type: type[T], *, maxsize: int = _DEFAULT_MAXSIZE) -> None:
        self.event_type = event_type
        self._queue: asyncio.Queue[T] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    @property
    def maxsize(self) -> int:
        return self._queue.maxsize

    def qsize(self) -> int:
        return self._queue.qsize()

    def offer(self, event: T) -> bool:
        """Non-blocking enqueue with drop-oldest backpressure.

        Returns True if accepted with no drop; False if the oldest queued
        event was dropped to make room (or, defensively, if put still fails).
        """
        dropped_this_call = False
        if self._queue.full():
            try:
                self._queue.get_nowait()
                self.dropped += 1
                dropped_this_call = True
                log.warning(
                    "subscription %s dropped oldest (total dropped=%d)",
                    self.event_type.__name__,
                    self.dropped,
                )
            except asyncio.QueueEmpty:
                pass
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1
            return False
        return not dropped_this_call

    async def get(self) -> T:
        return await self._queue.get()

    def get_nowait(self) -> T:
        return self._queue.get_nowait()

    def __aiter__(self) -> AsyncIterator[T]:
        return self

    async def __anext__(self) -> T:
        return await self._queue.get()


class EventBus:
    """In-memory pub/sub.

    Publishing is synchronous and non-blocking — failure mode is dropping,
    not blocking the producer. Subscribers consume asynchronously via
    `await sub.get()` or `async for event in sub`.
    """

    def __init__(self, *, default_maxsize: int = _DEFAULT_MAXSIZE) -> None:
        self._subscriptions: list[Subscription[Any]] = []
        self._default_maxsize = default_maxsize

    def subscribe[T](self, event_type: type[T], *, maxsize: int | None = None) -> Subscription[T]:
        sub = Subscription(event_type, maxsize=maxsize or self._default_maxsize)
        self._subscriptions.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription[Any]) -> None:
        self._subscriptions.remove(sub)

    def publish(self, event: Any) -> None:
        """Iterate subscriptions and offer the event to every one whose
        event_type is a class or superclass of the event's runtime type.
        """
        for sub in self._subscriptions:
            if isinstance(event, sub.event_type):
                sub.offer(event)

    @property
    def subscription_count(self) -> int:
        return len(self._subscriptions)
