"""BottomVolumeSpikeStrategy (K) — 60일 저점 + 거래량 spike → trial position.

book strategy.md 의 K:
- entry: 60일 저점 ±5% + 거래량 spike (직전 5분 평균 ×3, 또는 5-day avg ×3)
- exit: +5~10% 익절 / 60일 저점 -2% 이탈 손절
- hold: 분~일 단위 (mean reversion)
- 철학: 공포 끝에서 거래량 다시 뛴다. 1차 매수만 가볍게.

V1: SixtyDayLow event 받으면 BUY signal. Tick 추적으로 +X% 익절 / 저점-Y%
이탈 손절.
"""

from dataclasses import dataclass
from datetime import datetime

from ks_ws.domain import Side, Signal, Tick
from ks_ws.events import Event, SixtyDayLow
from ks_ws.strategies.base import Strategy


@dataclass
class _Position:
    entry_price: int
    entry_time: datetime
    low_price: int  # 60-day low at entry; tracking baseline


class BottomVolumeSpikeStrategy(Strategy):
    name = "bottom_volume_spike"

    def __init__(
        self,
        *,
        watchlist: set[str] | None = None,
        take_profit_pct: float = 7.0,
        stop_below_low_pct: float = 2.0,
        confidence: float = 0.4,
    ) -> None:
        if take_profit_pct <= 0 or stop_below_low_pct <= 0:
            raise ValueError("take_profit_pct and stop_below_low_pct must be positive")
        if not 0 < confidence <= 1:
            raise ValueError("confidence must be in (0, 1]")
        self.watchlist = set(watchlist) if watchlist else None
        self.take_profit_pct = take_profit_pct
        self.stop_below_low_pct = stop_below_low_pct
        self.confidence = confidence
        self._open: dict[str, _Position] = {}

    def on_event(self, event: Event) -> list[Signal]:
        if not isinstance(event, SixtyDayLow):
            return []
        if self.watchlist is not None and event.symbol not in self.watchlist:
            return []
        if event.symbol in self._open:
            return []
        self._open[event.symbol] = _Position(
            entry_price=event.current_price,
            entry_time=event.timestamp,
            low_price=event.low_price,
        )
        return [
            Signal(
                symbol=event.symbol,
                side=Side.BUY,
                confidence=self.confidence,
                strategy=self.name,
                timestamp=event.timestamp,
                note=(
                    f"60d low @ {event.low_price}, current @ {event.current_price} "
                    f"(+{event.band_pct:.1f}%), vol ×{event.volume_multiplier:.1f}"
                ),
            )
        ]

    def on_tick(self, tick: Tick) -> list[Signal]:
        pos = self._open.get(tick.symbol)
        if pos is None:
            return []
        tp = pos.entry_price * (1 + self.take_profit_pct / 100)
        sl = pos.low_price * (1 - self.stop_below_low_pct / 100)
        if tick.price >= tp:
            del self._open[tick.symbol]
            return [
                Signal(
                    symbol=tick.symbol, side=Side.SELL, confidence=1.0,
                    strategy=self.name, timestamp=tick.timestamp,
                    note=f"take-profit @ {tick.price}",
                )
            ]
        if tick.price <= sl:
            del self._open[tick.symbol]
            return [
                Signal(
                    symbol=tick.symbol, side=Side.SELL, confidence=1.0, urgency="high",
                    strategy=self.name, timestamp=tick.timestamp,
                    note=f"stop below 60d low @ {tick.price} (low={pos.low_price})",
                )
            ]
        return []

    def open_positions(self) -> dict[str, _Position]:
        return dict(self._open)
