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


class LimitUpReached(Event):
    """Stock price reached daily limit-up (+30% vs previous close).

    Emitted when the latest tick price >= the limit-up price for that
    symbol. ``limit_up_price`` is the broker-rounded threshold (KRX tick
    size aware). Followers in 짝꿍 매매 (pair trading) react to this for
    the leader.
    """

    limit_up_price: int
    prev_close: int


class LimitUpBroken(Event):
    """Stock that reached limit-up has fallen back below it (limit broke).

    Emitted within seconds of best_bid dropping below limit_up_price for
    a symbol previously in LimitUpReached state. Pair followers should
    immediately exit on this event (매수 기준 훼손).
    """

    limit_up_price: int
    current_price: int


class DojiCandle(Event):
    """Daily bar形成 a doji pattern (open ≈ close, small body relative to range).

    Emitted when |open - close| / open < body_pct_threshold AND the bar
    has meaningful range (high - low > 0). Doji = market indecision; in
    종가 베팅 (closing bet) the doji on a daily bar at session-close
    signals a potential resolve next day.
    """

    body_pct: float  # |open - close| / open * 100
    range_pct: float  # (high - low) / open * 100
    direction_hint: str  # "neutral" / "uptrend" / "downtrend" — context only
