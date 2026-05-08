"""VolumeSpikeStrategy — buys on a volume surge, exits on the next opposing
spike or via an explicit hold-bar window.

Reference implementation. Reads VolumeSpike events emitted by the
detector and emits a BUY signal whose confidence scales linearly with
the observed multiplier. Subscribers can configure ``confidence_floor``
and ``confidence_cap`` to fit their allocator's risk profile.

This is a **directional momentum** play — assumes a sudden volume burst
indicates buying interest. Pair with a separate exit strategy or rely
on the Allocator + Risk to size positions modestly.
"""

from datetime import UTC, datetime

from ks_ws.domain import Side, Signal
from ks_ws.events import Event, VolumeSpike
from ks_ws.strategies.base import Strategy


class VolumeSpikeStrategy(Strategy):
    name = "volume_spike"

    def __init__(
        self,
        *,
        confidence_cap_multiplier: float = 5.0,
        confidence_floor: float = 0.2,
        urgency: str = "high",
    ) -> None:
        if confidence_cap_multiplier <= 1.0:
            raise ValueError("confidence_cap_multiplier must be > 1.0")
        if not (0.0 <= confidence_floor <= 1.0):
            raise ValueError("confidence_floor must be in [0, 1]")
        self.confidence_cap_multiplier = confidence_cap_multiplier
        self.confidence_floor = confidence_floor
        self.urgency = urgency

    def on_event(self, event: Event) -> list[Signal]:
        if not isinstance(event, VolumeSpike):
            return []
        # Linear scale from threshold (multiplier ~ detector's threshold) up to cap.
        scaled = min(1.0, event.multiplier / self.confidence_cap_multiplier)
        confidence = max(self.confidence_floor, scaled)
        return [
            Signal(
                symbol=event.symbol,
                side=Side.BUY,
                confidence=confidence,
                urgency=self.urgency,  # type: ignore[arg-type]
                strategy=self.name,
                timestamp=datetime.now(UTC),
                note=f"volume spike {event.multiplier:.1f}x baseline",
            )
        ]
