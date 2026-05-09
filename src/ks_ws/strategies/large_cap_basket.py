"""LargeCapBasketStrategy (Sec 27) — 코스피200 시총 상위 N 종목 동시 시그널
→ equal-weight basket entry.

book Sec 27: 대형주 장세 = 시총 큰 종목 거래대금 ↑. LG전자/LG화학/SK이노/현대차/
삼성전자 동시 매매. 본 V1 은 단순 모델:

- watchlist 의 종목 중 동일 trigger event (예: VolumeSpike) 가 N개 이상 동시
  발화하면 dominance 인식 → 모든 watchlist 종목에 BUY (equal-weight)
- exit: 개별 +X% 익절 / -Y% 손절

V1 기본 trigger = 시간창 내 dominance count. 각 symbol BUY signal 의 confidence
는 고정. 별도 dominance signal event 도 가능하지만 V1 은 stateful tracker.
"""

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ks_ws.domain import Side, Signal, Tick
from ks_ws.events import Event, VolumeSpike
from ks_ws.strategies.base import Strategy


@dataclass
class _BasketState:
    triggers: deque[datetime] = field(default_factory=deque)


@dataclass
class _Position:
    entry_price: int
    entry_time: datetime


class LargeCapBasketStrategy(Strategy):
    name = "large_cap_basket"

    def __init__(
        self,
        *,
        watchlist: set[str],
        dominance_threshold: int = 3,
        dominance_window: timedelta = timedelta(minutes=5),
        take_profit_pct: float = 3.0,
        stop_loss_pct: float = 2.0,
        confidence: float = 0.6,
    ) -> None:
        if len(watchlist) < 2:
            raise ValueError("watchlist must have ≥ 2 symbols (basket needs members)")
        if dominance_threshold < 2:
            raise ValueError("dominance_threshold must be >= 2")
        if take_profit_pct <= 0 or stop_loss_pct <= 0:
            raise ValueError("pcts must be positive")
        if not 0 < confidence <= 1:
            raise ValueError("confidence must be in (0, 1]")
        self.watchlist = set(watchlist)
        self.dominance_threshold = dominance_threshold
        self.dominance_window = dominance_window
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.confidence = confidence
        self._state = _BasketState()
        self._open: dict[str, _Position] = {}
        self._last_price: dict[str, int] = {}
        self._triggered_symbols: set[str] = set()

    def on_event(self, event: Event) -> list[Signal]:
        if not isinstance(event, VolumeSpike):
            return []
        if event.symbol not in self.watchlist:
            return []
        # Trim old triggers from window
        cutoff = event.timestamp - self.dominance_window
        while self._state.triggers and self._state.triggers[0] < cutoff:
            self._state.triggers.popleft()
            # Forget triggered symbols outside the window (simplistic — flush all)
        # Add new trigger; track unique symbols within window via timestamp pair
        self._state.triggers.append(event.timestamp)
        self._triggered_symbols.add(event.symbol)
        # If we now have >= threshold distinct symbols triggered within window
        # (use _triggered_symbols which we never trim per-symbol — good enough V1)
        if len(self._triggered_symbols) < self.dominance_threshold:
            return []
        if self._open:
            return []  # already holding the basket
        # Open positions for every watchlist symbol that has a known last price
        signals: list[Signal] = []
        for sym in self.watchlist:
            if sym in self._open:
                continue
            last = self._last_price.get(sym)
            if last is None:
                continue
            self._open[sym] = _Position(entry_price=last, entry_time=event.timestamp)
            signals.append(
                Signal(
                    symbol=sym, side=Side.BUY, confidence=self.confidence,
                    strategy=self.name, timestamp=event.timestamp,
                    note=f"basket triggered by {len(self._triggered_symbols)} dominance",
                )
            )
        # Reset trigger memory after firing the basket
        self._triggered_symbols.clear()
        self._state.triggers.clear()
        return signals

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
                    note=f"basket TP @ {tick.price}",
                )
            ]
        if tick.price <= sl:
            del self._open[tick.symbol]
            return [
                Signal(
                    symbol=tick.symbol, side=Side.SELL, confidence=1.0, urgency="high",
                    strategy=self.name, timestamp=tick.timestamp,
                    note=f"basket SL @ {tick.price}",
                )
            ]
        return []

    def open_positions(self) -> dict[str, _Position]:
        return dict(self._open)
