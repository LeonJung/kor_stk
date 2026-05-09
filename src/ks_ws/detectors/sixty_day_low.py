"""SixtyDayLowDetector — emits SixtyDayLow when symbol price is within
``band_pct`` of its 60-day low AND a recent volume spike accompanied
the dip.

K BottomVolumeSpike strategy 의 entry trigger. 바닥권 = 매도 끝났을 가능성,
거래량 급증 = 매수 세력 진입. 둘 다 동시여야 trial position.

판정 (per symbol):
- 60-day rolling low (closing prices)
- current price within ``band_pct`` of low (default ±5%)
- recent N-bar volume sum > baseline ×``volume_multiplier`` (default ×3)
- 한번 emit 후 hysteresis: 가격이 band 위로 회복할 때까지 재emit 안 함
"""

from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field

from ks_ws.domain import Bar
from ks_ws.events import SixtyDayLow


@dataclass
class _SymbolWindow:
    closes: deque[int] = field(default_factory=deque)
    volumes: deque[int] = field(default_factory=deque)
    in_band: bool = False  # hysteresis flag


class SixtyDayLowDetector:
    def __init__(
        self,
        *,
        emit: Callable[[SixtyDayLow], None],
        window_days: int = 60,
        band_pct: float = 5.0,
        volume_window: int = 5,
        volume_multiplier: float = 3.0,
        timeframe: str = "1d",
    ) -> None:
        if window_days < 5:
            raise ValueError("window_days must be >= 5")
        if band_pct <= 0:
            raise ValueError("band_pct must be positive")
        if volume_window < 2:
            raise ValueError("volume_window must be >= 2")
        if volume_multiplier < 1:
            raise ValueError("volume_multiplier must be >= 1")
        self._emit = emit
        self.window_days = window_days
        self.band_pct = band_pct
        self.volume_window = volume_window
        self.volume_multiplier = volume_multiplier
        self.timeframe = timeframe
        self._windows: dict[str, _SymbolWindow] = defaultdict(_SymbolWindow)

    def feed_bar(self, bar: Bar) -> None:
        if bar.timeframe != self.timeframe:
            return
        w = self._windows[bar.symbol]
        w.closes.append(bar.close)
        w.volumes.append(bar.volume)
        # Trim
        while len(w.closes) > self.window_days:
            w.closes.popleft()
        while len(w.volumes) > self.window_days:
            w.volumes.popleft()
        if len(w.closes) < max(self.volume_window + 1, 5):
            return

        low = min(w.closes)
        upper_bound = low * (1 + self.band_pct / 100)
        in_band_now = bar.close <= upper_bound

        # Volume spike — recent window vs full-window avg (excluding recent)
        recent = list(w.volumes)[-self.volume_window:]
        prior = list(w.volumes)[: -self.volume_window]
        if not prior:
            return
        recent_sum = sum(recent)
        prior_avg = sum(prior) / len(prior)
        if prior_avg <= 0:
            return
        per_bar_recent = recent_sum / len(recent)
        spike = per_bar_recent / prior_avg

        if in_band_now and spike >= self.volume_multiplier and not w.in_band:
            w.in_band = True
            self._emit(
                SixtyDayLow(
                    symbol=bar.symbol,
                    timestamp=bar.timestamp,
                    low_price=low,
                    current_price=bar.close,
                    band_pct=(bar.close - low) / low * 100,
                    volume_multiplier=spike,
                )
            )
        elif not in_band_now and w.in_band:
            # price recovered above band; reset hysteresis so a future re-dip can fire
            w.in_band = False
