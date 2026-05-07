"""Base class for trading strategies.

A Strategy reacts to data and emits Signals. It does NOT decide order
size or place orders — sizing belongs to the Allocator and execution
belongs to the order layer. Keeping strategies as pure decision
functions makes them composable, swappable, and easy to backtest.

Subclass and override only the on_X methods you care about. The rest
return empty lists by default so a Strategy that only watches events
need not implement on_bar / on_tick.
"""

from ks_ws.domain import Bar, OrderBook, Signal, Tick
from ks_ws.events import Event


class Strategy:
    """Base for all strategies.

    Concrete strategies must set `name` (used for logging, weights, and
    Signal.strategy attribution) and override at least one on_X method.
    """

    name: str = "unnamed"

    def on_bar(self, bar: Bar) -> list[Signal]:
        return []

    def on_tick(self, tick: Tick) -> list[Signal]:
        return []

    def on_orderbook(self, orderbook: OrderBook) -> list[Signal]:
        return []

    def on_event(self, event: Event) -> list[Signal]:
        return []
