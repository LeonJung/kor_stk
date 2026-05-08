"""OrderbookImbalanceDetector — emits OrderbookImbalance when bid/ask
volume ratio over the top N levels crosses the configured threshold.

Imbalance ratio = (sum of top-N bid volumes) / (sum of top-N ask volumes).

Ratio > 1 means buy pressure; < 1 means sell pressure. Configurable
threshold can fire on either direction (use buy_threshold for >, and
sell_threshold for the inverse).

Per-symbol hysteresis prevents chatter near the threshold.
"""

from ks_ws.bus import EventBus
from ks_ws.domain import OrderBook
from ks_ws.events import OrderbookImbalance


class OrderbookImbalanceDetector:
    def __init__(
        self,
        bus: EventBus,
        *,
        levels: int = 5,
        buy_threshold: float = 2.0,
        cooldown_threshold: float = 1.3,
    ) -> None:
        if levels < 1:
            raise ValueError("levels must be >= 1")
        if buy_threshold <= cooldown_threshold:
            raise ValueError("buy_threshold must be strictly greater than cooldown_threshold")
        self._bus = bus
        self.levels = levels
        self.buy_threshold = buy_threshold
        self.cooldown_threshold = cooldown_threshold
        self._fired: dict[str, bool] = {}

    def feed(self, ob: OrderBook) -> None:
        bids = ob.bids[: self.levels]
        asks = ob.asks[: self.levels]
        bid_sum = sum(level.volume for level in bids)
        ask_sum = sum(level.volume for level in asks)
        if ask_sum <= 0:
            return  # no asks → can't compute ratio meaningfully
        ratio = bid_sum / ask_sum
        in_fire = self._fired.get(ob.symbol, False)
        if not in_fire and ratio >= self.buy_threshold:
            self._fired[ob.symbol] = True
            self._bus.publish(
                OrderbookImbalance(
                    symbol=ob.symbol,
                    timestamp=ob.timestamp,
                    bid_to_ask_ratio=ratio,
                    levels_used=self.levels,
                )
            )
        elif in_fire and ratio <= self.cooldown_threshold:
            self._fired[ob.symbol] = False
