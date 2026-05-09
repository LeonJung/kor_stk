"""MicroCapStrategy (Sec 14) — 소형주 매매 전략.

book Sec 14: 소형주 = 시총 작고 변동성 큼. 큰 비중 + 편안한 마인드 = 핵심.
조급하면 변동성에 흔들려 손절. 손절 라인 -1~-2% 엄격, 그 외엔 느긋.

V1 단순:
- watchlist 의 소형주만 처리 (시총 cap 외부 검증)
- VolumeSpike event 또는 OrderbookImbalance event 받으면 BUY (호재 진입)
- exit:
  - +X% 익절 (default +5%)
  - -1~-2% 엄격 손절
  - 시간 청산 없음 (느긋한 hold)

별도 risk_profile (시총 cap, 손절 -2% 엄격) 분리.
"""

from dataclasses import dataclass
from datetime import datetime

from ks_ws.domain import Side, Signal, Tick
from ks_ws.events import Event, OrderbookImbalance, VolumeSpike
from ks_ws.strategies.base import Strategy


@dataclass
class _Position:
    entry_price: int
    entry_time: datetime


class MicroCapStrategy(Strategy):
    name = "microcap"

    def __init__(
        self,
        *,
        watchlist: set[str],
        take_profit_pct: float = 5.0,
        stop_loss_pct: float = 1.5,
        confidence: float = 0.5,
    ) -> None:
        if not watchlist:
            raise ValueError("watchlist must not be empty")
        if take_profit_pct <= 0 or stop_loss_pct <= 0:
            raise ValueError("pcts must be positive")
        if stop_loss_pct > 3.0:
            raise ValueError("stop_loss_pct must be ≤ 3.0 (microcap requires strict stop)")
        if not 0 < confidence <= 1:
            raise ValueError("confidence must be in (0, 1]")
        self.watchlist = set(watchlist)
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.confidence = confidence
        self._open: dict[str, _Position] = {}
        self._last_price: dict[str, int] = {}

    def on_tick(self, tick: Tick) -> list[Signal]:
        self._last_price[tick.symbol] = tick.price
        pos = self._open.get(tick.symbol)
        if pos is None:
            return []
        tp = pos.entry_price * (1 + self.take_profit_pct / 100)
        sl = pos.entry_price * (1 - self.stop_loss_pct / 100)
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
                    note=f"strict stop @ {tick.price}",
                )
            ]
        return []

    def on_event(self, event: Event) -> list[Signal]:
        if not isinstance(event, (VolumeSpike, OrderbookImbalance)):
            return []
        if event.symbol not in self.watchlist:
            return []
        if event.symbol in self._open:
            return []
        # entry — use last known price (or skip if unknown)
        last = self._last_price.get(event.symbol)
        if last is None:
            return []
        self._open[event.symbol] = _Position(entry_price=last, entry_time=event.timestamp)
        return [
            Signal(
                symbol=event.symbol, side=Side.BUY, confidence=self.confidence,
                strategy=self.name, timestamp=event.timestamp,
                note=f"microcap entry on {type(event).__name__}",
            )
        ]

    def open_positions(self) -> dict[str, _Position]:
        return dict(self._open)
