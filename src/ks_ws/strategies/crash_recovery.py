"""CrashRecoveryStrategy (Sec 23) — 셀타링 (panic sell) 후 시총 대형주만
-X% 매수, 소형주는 매수 X.

book Sec 23: 시장이 패닉 (코스피 -2~-5%) 후 매도세가 진정될 때, **시총 대형주
(코스피200 안)** 만 신중히 매수. 소형주는 lacks liquidity → 매수 X.

V1 디자인:
- ManiaSignal 또는 별도 panic event 받기 (V1 = 직접 KOSPI bar drawdown 추적)
- KOSPI 일봉 drawdown ≥ panic_threshold_pct → panic state
- panic state 동안 caller-supplied large_cap_universe 의 종목만 매수 가능
- 가격 -recovery_buy_drawdown_pct 도달 시 BUY signal
- exit: +recovery_target_pct 익절 / -stop_pct 손절

V1 은 단순 stub: tick 받아 entry/exit. KOSPI drawdown 외부 주입 (panic_active
flag).
"""

from dataclasses import dataclass
from datetime import datetime

from ks_ws.domain import Side, Signal, Tick
from ks_ws.strategies.base import Strategy


@dataclass
class _Position:
    entry_price: int
    entry_time: datetime


class CrashRecoveryStrategy(Strategy):
    name = "crash_recovery"

    def __init__(
        self,
        *,
        large_cap_universe: set[str],
        recovery_target_pct: float = 5.0,
        stop_pct: float = 3.0,
        confidence: float = 0.5,
    ) -> None:
        if not large_cap_universe:
            raise ValueError("large_cap_universe must not be empty")
        if recovery_target_pct <= 0 or stop_pct <= 0:
            raise ValueError("pcts must be positive")
        if not 0 < confidence <= 1:
            raise ValueError("confidence must be in (0, 1]")
        self.large_cap_universe = set(large_cap_universe)
        self.recovery_target_pct = recovery_target_pct
        self.stop_pct = stop_pct
        self.confidence = confidence
        self._panic_active = False
        self._open: dict[str, _Position] = {}

    def set_panic(self, active: bool) -> None:
        """External setter — caller (KOSPI drawdown monitor or sidecar event
        consumer) toggles panic state."""
        self._panic_active = active

    def on_tick(self, tick: Tick) -> list[Signal]:
        # Exit logic first (always runs, even outside panic)
        pos = self._open.get(tick.symbol)
        if pos is not None:
            tp = pos.entry_price * (1 + self.recovery_target_pct / 100)
            sl = pos.entry_price * (1 - self.stop_pct / 100)
            if tick.price >= tp:
                del self._open[tick.symbol]
                return [
                    Signal(
                        symbol=tick.symbol, side=Side.SELL, confidence=1.0,
                        strategy=self.name, timestamp=tick.timestamp,
                        note=f"recovery TP @ {tick.price}",
                    )
                ]
            if tick.price <= sl:
                del self._open[tick.symbol]
                return [
                    Signal(
                        symbol=tick.symbol, side=Side.SELL, confidence=1.0, urgency="high",
                        strategy=self.name, timestamp=tick.timestamp,
                        note=f"recovery SL @ {tick.price}",
                    )
                ]
            return []
        # Entry only during active panic AND only large-cap symbols
        if not self._panic_active:
            return []
        if tick.symbol not in self.large_cap_universe:
            return []
        # Open at current tick price
        self._open[tick.symbol] = _Position(entry_price=tick.price, entry_time=tick.timestamp)
        return [
            Signal(
                symbol=tick.symbol, side=Side.BUY, confidence=self.confidence,
                strategy=self.name, timestamp=tick.timestamp,
                note=f"crash recovery buy (panic active)",
            )
        ]

    def open_positions(self) -> dict[str, _Position]:
        return dict(self._open)
