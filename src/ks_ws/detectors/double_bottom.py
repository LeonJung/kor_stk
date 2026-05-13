"""DoubleBottomDetector — W double-bottom 차트 패턴 (technical §J1).

만쥬 책 + alphasquare 자료 기반:
"하락 추세 → 저점1 → 반등 → 저점2 (저점1 ±2%) → 넥라인 (저점1·2 사이 고점)
돌파 + 거래량 spike → 매수. 20일 이평선이 W 모양 인지 결합 검증."

API:
- detect_double_bottom(bars, *, ...) → DoubleBottomResult | None — stateless
- DoubleBottomDetector — stateful, feed Bars, emit DoubleBottomDetected event

stateless detect:
- bars 마지막 N (default 60) 일봉 검사
- 저점1 = window 안 최저가 인덱스
- 저점2 = 저점1 이후 + 저점1 ±low_tolerance_pct (default 2%) 안 + 충분 분리 (min_separation_bars 5)
- 넥라인 = 저점1 과 저점2 사이 최고가
- 현재 봉 close > 넥라인 → DoubleBottom 완성 (BUY signal)

V1 = 가격 패턴만. 20일선 결합은 향후 보강.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ks_ws.bus import EventBus
from ks_ws.domain import Bar
from ks_ws.events import DoubleBottomDetected  # neckline + (neckline - low) — measured move


@dataclass
class DoubleBottomResult:
    low1_idx: int
    low2_idx: int
    neckline_idx: int
    low1_price: int
    low2_price: int
    neckline_price: int
    target_price: int


def detect_double_bottom(
    bars: Sequence[Bar],
    *,
    lookback: int = 60,
    low_tolerance_pct: float = 2.0,
    min_separation_bars: int = 5,
    max_separation_bars: int = 30,
    neckline_min_rise_pct: float = 3.0,
) -> DoubleBottomResult | None:
    """Detect a completed W double-bottom in the most recent ``lookback`` bars.

    Returns DoubleBottomResult if the last bar's close has broken above the
    neckline of a valid pattern. None otherwise.

    Pattern validity:
    - low1 = lowest low in window (excluding the last few bars where low2 must lie)
    - low2 = a lower low (index > low1) within ±low_tolerance_pct of low1
    - neckline = highest high between low1 and low2
    - neckline > low1 by at least neckline_min_rise_pct (no flat W's)
    - last bar's close > neckline (breakout completion)
    """
    if low_tolerance_pct <= 0 or neckline_min_rise_pct <= 0:
        raise ValueError("percentages must be positive")
    if min_separation_bars >= max_separation_bars:
        raise ValueError("min_separation must be < max_separation")
    if len(bars) < min_separation_bars + 2:
        return None

    window = list(bars[-lookback:])
    n = len(window)
    last_close = window[-1].close

    # Best (= lowest score) candidate
    best: DoubleBottomResult | None = None

    for i in range(n - min_separation_bars - 1):
        low1 = window[i].low
        tol_band = low1 * low_tolerance_pct / 100
        # Look for low2 in valid separation window
        for j in range(
            i + min_separation_bars,
            min(n - 1, i + max_separation_bars + 1),
        ):
            low2 = window[j].low
            if abs(low2 - low1) > tol_band:
                continue
            # Found candidate (i, j). Confirm neckline > both lows.
            between = window[i + 1 : j]
            if not between:
                continue
            neckline_bar_idx = max(range(len(between)), key=lambda k: between[k].high)
            neckline = between[neckline_bar_idx].high
            if neckline <= max(low1, low2):
                continue
            min_low = min(low1, low2)
            rise_pct = (neckline - min_low) / min_low * 100
            if rise_pct < neckline_min_rise_pct:
                continue
            # Pattern completes only on a bar that closes ABOVE neckline.
            if last_close <= neckline:
                continue
            # Build result (using window-local indices; caller can offset)
            result = DoubleBottomResult(
                low1_idx=i,
                low2_idx=j,
                neckline_idx=i + 1 + neckline_bar_idx,
                low1_price=low1,
                low2_price=low2,
                neckline_price=neckline,
                target_price=neckline + (neckline - min_low),
            )
            # Prefer the *deepest* valid pattern — lowest min(low1, low2).
            # Tie-break by most recent low2 (largest low2_idx).
            if best is None:
                best = result
            else:
                best_min = min(best.low1_price, best.low2_price)
                cand_min = min(result.low1_price, result.low2_price)
                if cand_min < best_min or (
                    cand_min == best_min and result.low2_idx > best.low2_idx
                ):
                    best = result
    return best


class DoubleBottomDetector:
    """Stateful adapter — feed daily Bars per symbol. Emits DoubleBottomDetected
    on neckline breakout. Hysteresis: re-emit only when a new low2_idx pattern
    forms (so same W pattern doesn't fire multiple times)."""

    def __init__(
        self,
        bus: EventBus,
        *,
        lookback: int = 60,
        low_tolerance_pct: float = 2.0,
        min_separation_bars: int = 5,
        max_separation_bars: int = 30,
        neckline_min_rise_pct: float = 3.0,
        publish: Callable[[DoubleBottomDetected], None] | None = None,
    ) -> None:
        self._bus = bus
        self.lookback = lookback
        self.low_tolerance_pct = low_tolerance_pct
        self.min_separation_bars = min_separation_bars
        self.max_separation_bars = max_separation_bars
        self.neckline_min_rise_pct = neckline_min_rise_pct
        self._publish = publish or (lambda ev: bus.publish(ev))
        self._bars: dict[str, list[Bar]] = {}
        self._last_emitted_low2_ts: dict[str, object] = {}
        self.emit_count = 0

    def feed(self, bar: Bar) -> None:
        bars = self._bars.setdefault(bar.symbol, [])
        bars.append(bar)
        if len(bars) > 2 * self.lookback:
            self._bars[bar.symbol] = bars[-2 * self.lookback :]
        result = detect_double_bottom(
            bars,
            lookback=self.lookback,
            low_tolerance_pct=self.low_tolerance_pct,
            min_separation_bars=self.min_separation_bars,
            max_separation_bars=self.max_separation_bars,
            neckline_min_rise_pct=self.neckline_min_rise_pct,
        )
        if result is None:
            return
        # Hysteresis: same pattern (same low2 timestamp) → no re-emit.
        window = bars[-self.lookback :]
        low2_ts = window[result.low2_idx].timestamp
        if self._last_emitted_low2_ts.get(bar.symbol) == low2_ts:
            return
        self._publish(
            DoubleBottomDetected(
                symbol=bar.symbol,
                timestamp=bar.timestamp,
                low1_price=result.low1_price,
                low2_price=result.low2_price,
                neckline_price=result.neckline_price,
                target_price=result.target_price,
            )
        )
        self._last_emitted_low2_ts[bar.symbol] = low2_ts
        self.emit_count += 1
