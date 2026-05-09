"""TrendShiftDetector — emit TrendShift event when market regime changes
or rolling strategy expectancy collapses.

book Sec 25:
- 시장 추세 전환 감지 → 매매 일시 조정
- 시그널: regime change + rolling expectancy 급락 + 주도 테마 교체

V1 디자인:
- update_regime(new_regime) 호출 → 이전 regime 과 다르면 TrendShift emit
- update_expectancy(rolling_expectancy_krw) → 직전 N tick rolling avg 대비
  drop_pct_threshold 이상 떨어지면 TrendShift emit (regime 변화 무관)
- 두 신호 중 하나만 발생해도 emit (or 강한 trigger)

caller 가 정기적으로 update_regime / update_expectancy 호출. emit 은 callback.
"""

from collections import deque
from collections.abc import Callable
from datetime import datetime
from statistics import fmean

from ks_ws.events import TrendShift


class TrendShiftDetector:
    def __init__(
        self,
        *,
        emit: Callable[[TrendShift], None],
        symbol: str = "MARKET",
        expectancy_window: int = 20,
        drop_pct_threshold: float = 30.0,
    ) -> None:
        if expectancy_window < 5:
            raise ValueError("expectancy_window must be >= 5")
        if drop_pct_threshold <= 0:
            raise ValueError("drop_pct_threshold must be positive")
        self._emit = emit
        self.symbol = symbol
        self.expectancy_window = expectancy_window
        self.drop_pct_threshold = drop_pct_threshold
        self._regime: str | None = None
        self._expectancies: deque[float] = deque(maxlen=expectancy_window * 2)

    def update_regime(self, new_regime: str, *, when: datetime) -> None:
        old = self._regime
        if old is not None and old != new_regime:
            self._emit(
                TrendShift(
                    symbol=self.symbol,
                    timestamp=when,
                    from_regime=old,
                    to_regime=new_regime,
                    expectancy_drop_pct=0.0,
                )
            )
        self._regime = new_regime

    def update_expectancy(self, expectancy_krw: float, *, when: datetime) -> None:
        self._expectancies.append(expectancy_krw)
        if len(self._expectancies) < self.expectancy_window:
            return
        # Compare last N/2 to prior N/2
        half = self.expectancy_window // 2
        recent = list(self._expectancies)[-half:]
        prior = list(self._expectancies)[-self.expectancy_window:-half]
        recent_avg = fmean(recent)
        prior_avg = fmean(prior)
        if prior_avg <= 0:
            return  # baseline near zero — undefined drop %
        drop_pct = (prior_avg - recent_avg) / prior_avg * 100
        if drop_pct >= self.drop_pct_threshold:
            self._emit(
                TrendShift(
                    symbol=self.symbol,
                    timestamp=when,
                    from_regime=self._regime or "unknown",
                    to_regime=self._regime or "unknown",
                    expectancy_drop_pct=-drop_pct,
                )
            )
            # Clear so we don't double-emit on same drop
            self._expectancies.clear()
