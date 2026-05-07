"""MarketDataHub — owns the data ingestion path and publishes to the EventBus.

Symbols are assigned to tiers, and each tier dictates the data path:

- HOT: WebSocket realtime — every tick / orderbook update. Limited slots
  (KIS allows ~41 simultaneous WS subscriptions per appkey), so reserve for
  the active position list and high-priority watchlist.
- WARM: REST polling at fixed interval — current price snapshots for
  symbols on the broader watchlist that don't fit in the HOT tier.
- COLD: REST batch (typically end-of-day) — daily bars only, for the
  long tail of the universe used for backtesting and screening.

The Hub is abstract — subclasses bind to actual data sources (real KIS
REST/WS or mock/replay sources). Bar / Tick / OrderBook records flow
out via `bus.publish(...)` so any consumer subscribes by class.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from enum import StrEnum

from ks_ws.bus import EventBus
from ks_ws.domain import Bar, OrderBook, Tick


class Tier(StrEnum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class MarketDataHub(ABC):
    """Abstract base for any data source that feeds the EventBus.

    Concrete subclasses implement start/stop and decide how to wire each
    tier to a real data path (or a fake one in tests).
    """

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._tiers: dict[str, Tier] = {}

    # Tier registry --------------------------------------------------------

    def assign(self, symbol: str, tier: Tier) -> None:
        self._tiers[symbol] = tier

    def assign_many(self, items: Iterable[tuple[str, Tier]]) -> None:
        for symbol, tier in items:
            self.assign(symbol, tier)

    def remove(self, symbol: str) -> None:
        self._tiers.pop(symbol, None)

    def tier_of(self, symbol: str) -> Tier | None:
        return self._tiers.get(symbol)

    def symbols_by_tier(self, tier: Tier) -> list[str]:
        return [s for s, t in self._tiers.items() if t == tier]

    @property
    def assignments(self) -> dict[str, Tier]:
        return dict(self._tiers)

    # Lifecycle ------------------------------------------------------------

    @abstractmethod
    async def start(self) -> None:
        """Open connections / start pollers. Idempotent."""

    @abstractmethod
    async def stop(self) -> None:
        """Close connections / cancel tasks. Idempotent."""


class MockMarketDataHub(MarketDataHub):
    """A no-op Hub useful for tests, replays, and pre-key development.

    Does not connect to anything. Tests / replay drivers call
    `feed_bar() / feed_tick() / feed_orderbook()` to inject records,
    which are then published to the bus exactly as a real Hub would.
    """

    def __init__(self, bus: EventBus) -> None:
        super().__init__(bus)
        self._started = False

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    def feed_bar(self, bar: Bar) -> None:
        self._bus.publish(bar)

    def feed_tick(self, tick: Tick) -> None:
        self._bus.publish(tick)

    def feed_orderbook(self, ob: OrderBook) -> None:
        self._bus.publish(ob)
