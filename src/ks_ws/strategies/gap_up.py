"""GapUpStrategy — buys on a meaningful upward gap, scaling confidence
with the gap size.

Reference implementation. The GapUp detector fires on session-opening
bars whose open exceeds the previous close by more than its
threshold; this strategy turns each event into a BUY signal whose
confidence scales linearly with the gap percentage up to
``gap_pct_cap`` (above which it saturates at 1.0).

Designed as a momentum entry — assumes the gap reflects new
information that the market hasn't fully priced. No automatic exit
here: combine with a time-based or counter-trend exit strategy.
"""

from datetime import UTC, datetime

from ks_ws.domain import Side, Signal
from ks_ws.events import Event, GapUp
from ks_ws.strategies.base import Strategy


class GapUpStrategy(Strategy):
    name = "gap_up"

    def __init__(
        self,
        *,
        gap_pct_cap: float = 10.0,
        confidence_floor: float = 0.3,
        urgency: str = "normal",
    ) -> None:
        if gap_pct_cap <= 0:
            raise ValueError("gap_pct_cap must be positive")
        if not (0.0 <= confidence_floor <= 1.0):
            raise ValueError("confidence_floor must be in [0, 1]")
        self.gap_pct_cap = gap_pct_cap
        self.confidence_floor = confidence_floor
        self.urgency = urgency

    def on_event(self, event: Event) -> list[Signal]:
        if not isinstance(event, GapUp):
            return []
        scaled = min(1.0, event.gap_pct / self.gap_pct_cap)
        confidence = max(self.confidence_floor, scaled)
        return [
            Signal(
                symbol=event.symbol,
                side=Side.BUY,
                confidence=confidence,
                urgency=self.urgency,  # type: ignore[arg-type]
                strategy=self.name,
                timestamp=datetime.now(UTC),
                note=f"gap up {event.gap_pct:.1f}%",
            )
        ]
