"""DojiCandleDetector — emits DojiCandle event when a daily bar shows a
doji pattern (small body relative to range).

도지 = 시초가 ≈ 종가. 매수/매도 균형 → 다음 거래일 방향 결정 임박. 종가 베팅
strategy 의 entry trigger 후보.

판정:
- body_pct = |open - close| / open * 100 < body_pct_threshold (default 0.3%)
- range_pct = (high - low) / open * 100 > range_pct_min (default 0.5%) — 거래
  활동이 있어야 의미 있음 (low-volume 도지는 무시)

direction_hint 는 (close - open) 기호 + 직전 추세 정보 없이 "neutral" 으로만
emit. Strategy 가 추가 컨텍스트 (장기 추세 등) 와 결합해 매수/매도 결정.
"""

from collections.abc import Callable

from ks_ws.domain import Bar
from ks_ws.events import DojiCandle


class DojiCandleDetector:
    def __init__(
        self,
        *,
        emit: Callable[[DojiCandle], None],
        body_pct_threshold: float = 0.3,
        range_pct_min: float = 0.5,
        timeframe: str = "1d",
    ) -> None:
        if body_pct_threshold <= 0:
            raise ValueError("body_pct_threshold must be positive")
        if range_pct_min <= 0:
            raise ValueError("range_pct_min must be positive")
        self._emit = emit
        self.body_pct_threshold = body_pct_threshold
        self.range_pct_min = range_pct_min
        self.timeframe = timeframe

    def feed_bar(self, bar: Bar) -> None:
        if bar.timeframe != self.timeframe:
            return
        if bar.open <= 0:
            return
        body_pct = abs(bar.open - bar.close) / bar.open * 100
        range_pct = (bar.high - bar.low) / bar.open * 100
        if body_pct >= self.body_pct_threshold:
            return
        if range_pct < self.range_pct_min:
            return
        self._emit(
            DojiCandle(
                symbol=bar.symbol,
                timestamp=bar.timestamp,
                body_pct=body_pct,
                range_pct=range_pct,
                direction_hint="neutral",
            )
        )
