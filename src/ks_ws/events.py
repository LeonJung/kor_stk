"""Detector events.

Detectors observe market data and emit typed events. Strategies subscribe
to the event types they care about. A new "edge" = a new Event subclass
+ a detector that emits it; strategies do not change.

All events are immutable. `event_type` returns the subclass name for
serialization / logging — `isinstance` is the right tool for dispatch
in code.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    timestamp: datetime

    @property
    def event_type(self) -> str:
        return type(self).__name__


class ProgramFlowEnter(Event):
    """Net program-trade buy flow crossed the entry threshold over a rolling window."""

    delta_krw: int  # net buy flow (KRW), positive = net buying
    window_seconds: int


class ProgramFlowExit(Event):
    """Net program-trade flow fell below the exit threshold (trend reversal)."""

    delta_krw: int
    window_seconds: int


class VolumeSpike(Event):
    """Trade volume in the window exceeded `multiplier` * baseline volume."""

    multiplier: float
    window_seconds: int


class OrderbookImbalance(Event):
    """Bid/ask volume ratio over the top N levels crossed the threshold."""

    bid_to_ask_ratio: float  # > 1 = buy pressure
    levels_used: int


class GapUp(Event):
    """Open price gapped up vs previous close beyond the threshold percentage."""

    gap_pct: float
