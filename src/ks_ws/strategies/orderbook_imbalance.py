"""OrderbookImbalanceStrategy — buys when bid pressure dominates by a wide
margin, optionally sells when the imbalance evaporates.

Reference implementation. The detector publishes OrderbookImbalance
events when the top-N bid/ask volume ratio exceeds buy_threshold. This
strategy turns each such event into a BUY signal whose confidence
scales with the observed ratio, capped at 1.0 at ``ratio_cap``.

Set ``sell_on_reversal=True`` to also emit a SELL when the ratio
inverts (configured by the detector's cooldown_threshold). Useful when
combined with another strategy holding the long.
"""

from datetime import UTC, datetime

from ks_ws.domain import Side, Signal
from ks_ws.events import Event, OrderbookImbalance
from ks_ws.strategies.base import Strategy


class OrderbookImbalanceStrategy(Strategy):
    name = "orderbook_imbalance"

    def __init__(
        self,
        *,
        ratio_cap: float = 5.0,
        confidence_floor: float = 0.3,
        urgency: str = "normal",
    ) -> None:
        if ratio_cap <= 1.0:
            raise ValueError("ratio_cap must be > 1.0")
        if not (0.0 <= confidence_floor <= 1.0):
            raise ValueError("confidence_floor must be in [0, 1]")
        self.ratio_cap = ratio_cap
        self.confidence_floor = confidence_floor
        self.urgency = urgency

    def on_event(self, event: Event) -> list[Signal]:
        if not isinstance(event, OrderbookImbalance):
            return []
        scaled = min(1.0, event.bid_to_ask_ratio / self.ratio_cap)
        confidence = max(self.confidence_floor, scaled)
        return [
            Signal(
                symbol=event.symbol,
                side=Side.BUY,
                confidence=confidence,
                urgency=self.urgency,  # type: ignore[arg-type]
                strategy=self.name,
                timestamp=datetime.now(UTC),
                note=f"book imbalance {event.bid_to_ask_ratio:.1f}",
            )
        ]
