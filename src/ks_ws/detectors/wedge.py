"""WedgeDetector — 상승/하락 웨지 (technical §J13-J14).

- Rising wedge: 고점·저점 모두 상승 + 저점 기울기가 더 가파름 → bearish reversal
                  (현물 BUY signal X)
- Falling wedge: 고점·저점 모두 하락 + 고점 기울기가 더 가파름 → bullish reversal
                  (BUY signal)

V1 detection (단순):
- bars 마지막 lookback (default 20) 봉
- highs first vs last (window thirds)
- lows first vs last
- Falling wedge: highs down + lows down + |highs slope| > |lows slope| (고점이 더
                  빨리 하락 = 수렴) + 마지막 close > 마지막 third high (돌파)
- Rising wedge: highs up + lows up + |lows slope| > |highs slope| + 마지막 close
                  < 마지막 third low (하향 이탈)

V1 = falling wedge (bullish reversal) 만 BUY signal 발화. rising wedge 도 emit
하지만 strategy 가 무시 (현물 매수 X).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ks_ws.bus import EventBus
from ks_ws.domain import Bar
from ks_ws.events import Event


class WedgeDetected(Event):
    """Wedge pattern broken in expected direction (technical J13/J14)."""

    wedge_type: str  # "falling" / "rising"
    upper_first: int  # first-third max high
    upper_last: int   # last-third max high
    lower_first: int
    lower_last: int
    breakout_price: int
    direction: str  # "up" / "down"


@dataclass
class WedgeResult:
    wedge_type: str
    upper_first: int
    upper_last: int
    lower_first: int
    lower_last: int
    breakout_price: int
    direction: str


def detect_wedge(
    bars: Sequence[Bar],
    *,
    lookback: int = 20,
    min_slope_pct: float = 3.0,
    slope_dominance_ratio: float = 1.3,
) -> WedgeResult | None:
    """Detect rising or falling wedge broken on the last bar.

    Falling wedge (bullish reversal):
    - upper_last < upper_first by ≥ min_slope_pct
    - lower_last < lower_first by ≥ min_slope_pct
    - |upper slope %| ≥ |lower slope %| * slope_dominance_ratio
    - last close > upper_last → upward break

    Rising wedge (bearish reversal):
    - upper_last > upper_first by ≥ min_slope_pct
    - lower_last > lower_first by ≥ min_slope_pct
    - |lower slope %| ≥ |upper slope %| * slope_dominance_ratio
    - last close < lower_last → downward break
    """
    if lookback < 9:
        raise ValueError("lookback must be at least 9")
    if min_slope_pct <= 0 or slope_dominance_ratio <= 0:
        raise ValueError("parameters must be positive")
    if len(bars) < lookback:
        return None

    window = list(bars[-lookback:])
    third = lookback // 3
    first = window[:third]
    last_third = window[-third - 1 : -1]  # exclude breakout bar
    last = window[-1]

    upper_first = max(b.high for b in first)
    lower_first = min(b.low for b in first)
    upper_last = max(b.high for b in last_third)
    lower_last = min(b.low for b in last_third)
    if upper_first <= 0 or lower_first <= 0:
        return None

    upper_slope_pct = (upper_last - upper_first) / upper_first * 100
    lower_slope_pct = (lower_last - lower_first) / lower_first * 100

    # Falling wedge: both slopes negative, upper slope dominant + breakout up.
    if (
        upper_slope_pct <= -min_slope_pct
        and lower_slope_pct <= -min_slope_pct
        and abs(upper_slope_pct) >= abs(lower_slope_pct) * slope_dominance_ratio
        and last.close > upper_last
    ):
        return WedgeResult(
            wedge_type="falling",
            upper_first=upper_first, upper_last=upper_last,
            lower_first=lower_first, lower_last=lower_last,
            breakout_price=last.close,
            direction="up",
        )

    # Rising wedge: both slopes positive, lower slope dominant + breakout down.
    if (
        upper_slope_pct >= min_slope_pct
        and lower_slope_pct >= min_slope_pct
        and lower_slope_pct >= upper_slope_pct * slope_dominance_ratio
        and last.close < lower_last
    ):
        return WedgeResult(
            wedge_type="rising",
            upper_first=upper_first, upper_last=upper_last,
            lower_first=lower_first, lower_last=lower_last,
            breakout_price=last.close,
            direction="down",
        )

    return None


class WedgeDetector:
    """Stateful — feed Bars, emit WedgeDetected on wedge breakout.
    Hysteresis: same (wedge_type, direction, upper_last) no re-emit."""

    def __init__(
        self,
        bus: EventBus,
        *,
        lookback: int = 20,
        min_slope_pct: float = 3.0,
        slope_dominance_ratio: float = 1.3,
        publish: Callable[[WedgeDetected], None] | None = None,
    ) -> None:
        self._bus = bus
        self.lookback = lookback
        self.min_slope_pct = min_slope_pct
        self.slope_dominance_ratio = slope_dominance_ratio
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
        result = detect_wedge(
            bars,
            lookback=self.lookback,
            min_slope_pct=self.min_slope_pct,
            slope_dominance_ratio=self.slope_dominance_ratio,
        )
        if result is None:
            return
        sig = (result.wedge_type, result.direction, result.upper_last)
        if self._last_emitted_signature.get(bar.symbol) == sig:
            return
        self._publish(
            WedgeDetected(
                symbol=bar.symbol,
                timestamp=bar.timestamp,
                wedge_type=result.wedge_type,
                upper_first=result.upper_first,
                upper_last=result.upper_last,
                lower_first=result.lower_first,
                lower_last=result.lower_last,
                breakout_price=result.breakout_price,
                direction=result.direction,
            )
        )
        self._last_emitted_signature[bar.symbol] = sig
        self.emit_count += 1
