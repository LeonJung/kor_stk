"""In-memory pub/sub event bus.

Topic = event class. Subscribers receive events whose runtime type is the
requested class or a subclass (isinstance dispatch). Each subscription
has a bounded queue; on overflow the oldest event is dropped so the
newest data wins — live trading favors freshness over completeness.

Assumes a single asyncio event loop. No additional thread safety
beyond what asyncio.Queue itself provides.

Shutdown: call `Subscription.close()` to stop a single subscriber
(awaiters of `get()` / `__anext__` raise StopAsyncIteration). Call
`EventBus.close()` to close every subscription and clear the registry.
Single consumer per Subscription is assumed; multiple concurrent
awaiters of the same subscription is unsupported.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

log = logging.getLogger("ks_ws.bus")

_DEFAULT_MAXSIZE = 1024


class _SentinelType:
    """Marker value put on a queue when its subscription is closed."""


_END = _SentinelType()


class Subscription[T]:
    """A bounded async queue subscribed to one event type."""

    def __init__(self, event_type: type[T], *, maxsize: int = _DEFAULT_MAXSIZE) -> None:
        self.event_type = event_type
        self._queue: asyncio.Queue[T | _SentinelType] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self._closed = False

    @property
    def maxsize(self) -> int:
        return self._queue.maxsize

    @property
    def closed(self) -> bool:
        return self._closed

    def qsize(self) -> int:
        return self._queue.qsize()

    def close(self) -> None:
        """Idempotent. Drops queued items if needed to enqueue an end-sentinel
        so any awaiter of get() / __anext__ unblocks with StopAsyncIteration.
        """
        if self._closed:
            return
        self._closed = True
        # Force the sentinel in. If full, drop one to make room.
        while True:
            try:
                self._queue.put_nowait(_END)
                return
            except asyncio.QueueFull:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    return  # nothing more we can do

    def offer(self, event: T) -> bool:
        """Non-blocking enqueue with drop-oldest backpressure.

        Returns True if accepted with no drop; False if a drop occurred or if
        the subscription is closed (event silently discarded).
        """
        if self._closed:
            return False
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
        item = await self._queue.get()
        if isinstance(item, _SentinelType):
            # Re-enqueue so any concurrent / subsequent awaiter also unblocks.
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(_END)
            raise StopAsyncIteration
        return item

    def get_nowait(self) -> T:
        item = self._queue.get_nowait()
        if isinstance(item, _SentinelType):
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(_END)
            raise StopAsyncIteration
        return item

    def __aiter__(self) -> AsyncIterator[T]:
        return self

    async def __anext__(self) -> T:
        return await self.get()


class EventBus:
    """In-memory pub/sub.

    Publishing is synchronous and non-blocking — failure mode is dropping,
    not blocking the producer. Subscribers consume asynchronously via
    `await sub.get()` or `async for event in sub`.
    """

    def __init__(self, *, default_maxsize: int = _DEFAULT_MAXSIZE) -> None:
        self._subscriptions: list[Subscription[Any]] = []
        self._default_maxsize = default_maxsize
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def subscribe[T](self, event_type: type[T], *, maxsize: int | None = None) -> Subscription[T]:
        if self._closed:
            raise RuntimeError("EventBus is closed")
        sub = Subscription(event_type, maxsize=maxsize or self._default_maxsize)
        self._subscriptions.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription[Any]) -> None:
        self._subscriptions.remove(sub)

    def publish(self, event: Any) -> None:
        """Iterate subscriptions and offer the event to every one whose
        event_type is a class or superclass of the event's runtime type.

        Closed subscriptions silently drop. Closed bus is a no-op (no raise).
        """
        if self._closed:
            return
        for sub in self._subscriptions:
            if isinstance(event, sub.event_type):
                sub.offer(event)

    def close(self) -> None:
        """Idempotent. Closes every Subscription and clears the registry."""
        if self._closed:
            return
        self._closed = True
        for sub in list(self._subscriptions):
            sub.close()
        self._subscriptions.clear()

    @property
    def subscription_count(self) -> int:
        return len(self._subscriptions)
