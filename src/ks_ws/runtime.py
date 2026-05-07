"""Runtime — wires the bus, strategies, and allocator into a running loop.

Subscribes to Bar / Tick / OrderBook / Event on the EventBus once each,
fans out received items to every registered Strategy by calling the
matching `on_X` method, collects emitted Signals, feeds them to the
Allocator, and publishes the resulting OrderIntents back onto the bus
for downstream consumers (OrderRouter, persistence, logging).

Two operating modes share the same dispatch logic:

- `step()` — synchronous one-shot drain of all currently queued items.
  Used by tests and by the backtest driver where time is advanced by
  the caller.
- `start()` / `stop()` — continuous async mode with one background task
  per topic. Used in live trading.

Single asyncio loop is assumed. Strategies run synchronously inside the
dispatch path; a slow strategy will hold up its peers. Per-strategy
budgets / process isolation are future work.
"""

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from typing import Any

from ks_ws.bus import EventBus, Subscription
from ks_ws.domain import Bar, OrderBook, Signal, Tick
from ks_ws.events import Event
from ks_ws.strategies.allocator import Allocator
from ks_ws.strategies.base import Strategy

log = logging.getLogger("ks_ws.runtime")


class Runtime:
    def __init__(
        self,
        bus: EventBus,
        strategies: Iterable[Strategy],
        allocator: Allocator,
    ) -> None:
        self._bus = bus
        self._strategies: list[Strategy] = list(strategies)
        self._allocator = allocator
        self._handlers: list[tuple[Subscription[Any], str]] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def setup(self) -> None:
        """Idempotent. Subscribes to bus topics so the dispatch loops have
        something to consume. Must be called (directly or via start()) before
        any market data is published, or early data will be missed.
        """
        if self._handlers:
            return
        self._handlers = [
            (self._bus.subscribe(Bar), "on_bar"),
            (self._bus.subscribe(Tick), "on_tick"),
            (self._bus.subscribe(OrderBook), "on_orderbook"),
            (self._bus.subscribe(Event), "on_event"),
        ]

    def step(self) -> int:
        """Drain every subscription synchronously and dispatch. Returns the
        total number of items processed across all topics. Useful for tests
        and for backtest drivers that advance time deterministically.
        """
        if not self._handlers:
            self.setup()
        count = 0
        for sub, method_name in self._handlers:
            while sub.qsize() > 0:
                try:
                    item = sub.get_nowait()
                except StopAsyncIteration:
                    break
                self._dispatch(item, method_name)
                count += 1
        return count

    async def start(self) -> None:
        """Continuous mode. Spins up one task per topic that consumes the
        subscription forever (until close())."""
        if self._running:
            return
        if not self._handlers:
            self.setup()
        self._running = True
        self._tasks = [
            asyncio.create_task(self._loop(sub, method_name)) for sub, method_name in self._handlers
        ]

    async def stop(self) -> None:
        """Close subscriptions (which drains async-for loops via sentinel),
        then await every dispatch task. Idempotent.
        """
        if not self._running:
            return
        self._running = False
        for sub, _ in self._handlers:
            sub.close()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._handlers = []
        self._tasks = []

    async def _loop(self, sub: Subscription[Any], method_name: str) -> None:
        async for item in sub:
            self._dispatch(item, method_name)

    def _dispatch(self, item: Any, method_name: str) -> None:
        signals: list[Signal] = []
        for strategy in self._strategies:
            method = getattr(strategy, method_name)
            try:
                emitted = method(item)
            except Exception:
                log.exception(
                    "strategy %s.%s raised; skipping",
                    strategy.name,
                    method_name,
                )
                continue
            if emitted:
                signals.extend(emitted)

        if not signals:
            return
        intents = self._allocator.combine(signals)
        for intent in intents:
            self._bus.publish(intent)
