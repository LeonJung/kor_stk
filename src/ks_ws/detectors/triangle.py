"""TriangleDetector — 대칭/상승/하강 삼각수렴 (technical §J7-J9).

3 variants:
- ascending: 수평 저항 + 상승 지지선 (highs flat, lows up) → bullish continuation
- descending: 수평 지지 + 하락 저항선 (highs down, lows flat) → bearish continuation
- symmetrical: 양쪽 수렴 (highs down, lows up) → 돌파 방향 따라

V1 detection:
- bars 마지막 lookback (default 20) 봉
- highs first vs last (window 의 첫 1/3 max high vs 마지막 1/3 max high):
  - last 가 first 보다 trend_threshold_pct (default 2%) 이상 작음 → "down"
  - 이상 큼 → "up"
  - else → "flat"
- lows 같은 방식
- variant 분류:
  - highs flat / lows up → ascending
  - highs down / lows flat → descending
  - highs down / lows up → symmetrical
- 추가: 마지막 봉 close > last 1/3 max high → breakout up
                       close < last 1/3 min low → breakout down

V1 단순화 — 실제 trend line fit 없이 thirds 비교만.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ks_ws.bus import EventBus
from ks_ws.domain import Bar
from ks_ws.events import TriangleDetected


@dataclass
class TriangleResult:
    variant: str  # "ascending" / "descending" / "symmetrical"
    apex_high: int
    apex_low: int
    breakout_price: int
    direction: str  # "up" / "down"


def _trend(first_value: int, last_value: int, threshold_pct: float) -> str:
    if first_value <= 0:
        return "flat"
    diff_pct = (last_value - first_value) / first_value * 100
    if diff_pct >= threshold_pct:
        return "up"
    if diff_pct <= -threshold_pct:
        return "down"
    return "flat"


def detect_triangle(
    bars: Sequence[Bar],
    *,
    lookback: int = 20,
    trend_threshold_pct: float = 2.0,
) -> TriangleResult | None:
    """Detect a triangle pattern broken on the last bar."""
    if lookback < 9:
        raise ValueError("lookback must be at least 9")
    if trend_threshold_pct <= 0:
        raise ValueError("trend_threshold_pct must be positive")
    if len(bars) < lookback:
        return None

    window = list(bars[-lookback:])
    third = lookback // 3
    first = window[:third]
    last_third = window[-third - 1 : -1]  # exclude breakout bar
    last = window[-1]

    first_high = max(b.high for b in first)
    last_high = max(b.high for b in last_third)
    first_low = min(b.low for b in first)
    last_low = min(b.low for b in last_third)

    high_trend = _trend(first_high, last_high, trend_threshold_pct)
    low_trend = _trend(first_low, last_low, trend_threshold_pct)

    variant: str | None = None
    if high_trend == "flat" and low_trend == "up":
        variant = "ascending"
    elif high_trend == "down" and low_trend == "flat":
        variant = "descending"
    elif high_trend == "down" and low_trend == "up":
        variant = "symmetrical"
    else:
        return None  # not a triangle (e.g., expanding, no convergence)

    apex_high = last_high
    apex_low = last_low

    # Breakout detection
    direction: str | None
    if last.close > apex_high:
        direction = "up"
    elif last.close < apex_low:
        direction = "down"
    else:
        return None  # no breakout this bar

    return TriangleResult(
        variant=variant,
        apex_high=apex_high,
        apex_low=apex_low,
        breakout_price=last.close,
        direction=direction,
    )


class TriangleDetector:
    """Stateful — feed Bars, emit TriangleDetected on breakout.

    Hysteresis: same (variant, direction, apex_high) signature no re-emit.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        lookback: int = 20,
        trend_threshold_pct: float = 2.0,
        publish: Callable[[TriangleDetected], None] | None = None,
    ) -> None:
        self._bus = bus
        self.lookback = lookback
        self.trend_threshold_pct = trend_threshold_pct
        self._publish = publish or (lambda ev: bus.publish(ev))
        self._bars: dict[str, list[Bar]] = {}
        self._last_emitted_signature: dict[str, tuple[str, str, int]] = {}
        self.emit_count = 0

    def feed(self, bar: Bar) -> None:
        bars = self._bars.setdefault(bar.symbol, [])
        bars.append(bar)
        cap = 3 * self.lookback
        if len(bars) > cap:
            self._bars[bar.symbol] = bars[-cap:]
        result = detect_triangle(
            bars,
            lookback=self.lookback,
            trend_threshold_pct=self.trend_threshold_pct,
        )
        if result is None:
            return
        sig = (result.variant, result.direction, result.apex_high)
        if self._last_emitted_signature.get(bar.symbol) == sig:
            return
        self._publish(
            TriangleDetected(
                symbol=bar.symbol,
                timestamp=bar.timestamp,
                variant=result.variant,
                apex_high=result.apex_high,
                apex_low=result.apex_low,
                breakout_price=result.breakout_price,
                direction=result.direction,
            )
        )
        self._last_emitted_signature[bar.symbol] = sig
        self.emit_count += 1
