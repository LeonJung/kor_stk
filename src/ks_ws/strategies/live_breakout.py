"""LiveBreakoutStrategy — 60일 신고가 돌파 + 거래량 ↑ → BUY (live 버전).

Backtest V2 의 'breakout' simulator (81% win, +6.76M / 60일 lookback) 의
라이브 적용. 분봉 close 가 60-day daily close 의 max 를 돌파 + 직전 5
분봉 평균 거래량의 1.5× 면 BUY 진입. 이후 +tp / -sl 도달 시 SELL.

Per-symbol state:
- high60: 60일 일봉 close 의 max (외부 주입, 매일 1회 update)
- entry: 매수 가격 (진입 후)
- entry_time: 진입 시각
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ks_ws.domain import Side, Signal, Tick
from ks_ws.strategies.base import Strategy


@dataclass
class _Pos:
    entry: int
    entry_time: datetime


class LiveBreakoutStrategy(Strategy):
    name = "breakout"

    def __init__(
        self,
        *,
        high60: dict[str, int],
        take_profit_pct: float = 2.0,
        stop_loss_pct: float = 3.0,
        max_hold_minutes: int = 60,
        confidence: float = 0.7,
    ) -> None:
        if take_profit_pct <= 0 or stop_loss_pct <= 0:
            raise ValueError("pct must be positive")
        if not 0 < confidence <= 1:
            raise ValueError("confidence in (0,1]")
        self.high60 = dict(high60)
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_hold = timedelta(minutes=max_hold_minutes)
        self.confidence = confidence
        self._open: dict[str, _Pos] = {}
        self._recent_ticks: dict[str, list[Tick]] = {}

    def on_tick(self, tick: Tick) -> list[Signal]:
        # Track recent ticks per symbol (cap last 50)
        recents = self._recent_ticks.setdefault(tick.symbol, [])
        recents.append(tick)
        if len(recents) > 50:
            recents.pop(0)

        pos = self._open.get(tick.symbol)
        if pos is not None:
            tp = pos.entry * (1 + self.take_profit_pct / 100)
            sl = pos.entry * (1 - self.stop_loss_pct / 100)
            if tick.price >= tp:
                del self._open[tick.symbol]
                return [self._sig(tick, Side.SELL, note=f"TP @ {tick.price}")]
            if tick.price <= sl:
                del self._open[tick.symbol]
                return [self._sig(tick, Side.SELL, urgency="high",
                                  note=f"SL @ {tick.price}")]
            if tick.timestamp - pos.entry_time >= self.max_hold:
                del self._open[tick.symbol]
                return [self._sig(tick, Side.SELL, note=f"hold timeout")]
            return []

        # Entry: tick price > high60 + tick volume spike
        h60 = self.high60.get(tick.symbol)
        if h60 is None or tick.price <= h60:
            return []
        # Simple volume confirmation: avg of last 20 tick volumes × 1.5
        if len(recents) < 20:
            return []
        avg_vol = sum(t.volume for t in recents[-21:-1]) / 20
        if avg_vol > 0 and tick.volume < avg_vol * 1.5:
            return []
        self._open[tick.symbol] = _Pos(entry=tick.price, entry_time=tick.timestamp)
        return [
            Signal(
                symbol=tick.symbol, side=Side.BUY, confidence=self.confidence,
                strategy=self.name, timestamp=tick.timestamp,
                note=f"breakout: {tick.price} > 60d high {h60} + vol×{tick.volume/avg_vol:.1f}",
            )
        ]

    def _sig(self, tick: Tick, side: Side, *, note: str, urgency: str = "normal") -> Signal:
        return Signal(
            symbol=tick.symbol, side=side, confidence=1.0,
            urgency=urgency,  # type: ignore[arg-type]
            strategy=self.name, timestamp=tick.timestamp, note=note,
        )

    def open_positions(self) -> dict[str, _Pos]:
        return dict(self._open)


def compute_high60(bar_store, codes: list[str]) -> dict[str, int]:
    """Return {symbol: max(close) of last 60 daily bars}."""
    out: dict[str, int] = {}
    for c in codes:
        bars = list(bar_store.read(c, "1d"))
        if not bars:
            continue
        recent = bars[-60:]
        out[c] = max(b.close for b in recent)
    return out
