"""BoxBreakoutDetector — N일 박스권 + 거래량 spike + 저항 돌파 (technical §J4).

만쥬 책 + 주덕 채널 사상 결합:
"일정 가격 범위 N일 횡보 후 저항 돌파 + 거래량 spike → 매수.
저항 돌파 후 retest 시 매수 (옛 저항 = 새 지지)."

검증:
- box_days = 횡보 일수 (default 10)
- box_range_pct = 박스권 high/low 폭 (default 4%)
- volume_multiplier = 돌파일 거래량 / 박스 평균 (default 2.0)
- breakout = 마지막 봉 close > 박스권 high

API:
- detect_box_breakout(bars, ...) → BoxBreakoutResult | None — stateless
- BoxBreakoutDetector — stateful, feed Bars, emit BoxBreakoutDetected event
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ks_ws.bus import EventBus
from ks_ws.domain import Bar
from ks_ws.events import BoxBreakoutDetected


@dataclass
class BoxBreakoutResult:
    box_high: int
    box_low: int
    box_days: int
    breakout_price: int
    volume_multiplier: float


def detect_box_breakout(
    bars: Sequence[Bar],
    *,
    box_days: int = 10,
    box_range_pct: float = 4.0,
    volume_multiplier_min: float = 2.0,
) -> BoxBreakoutResult | None:
    """Detect an N-day box range broken on the last bar with volume.

    Pattern:
    - Last bar is the breakout candidate.
    - Previous ``box_days`` bars (excluding last) have high/low spanning
      ≤ ``box_range_pct``% of mean price.
    - Last bar's close > box_high (breakout).
    - Last bar's volume ≥ box_avg_volume * volume_multiplier_min.
    """
    if box_days <= 0 or box_range_pct <= 0 or volume_multiplier_min <= 0:
        raise ValueError("parameters must be positive")
    if len(bars) < box_days + 1:
        return None

    window = list(bars[-(box_days + 1):])
    box_bars = window[:-1]  # previous N days
    last = window[-1]

    box_high = max(b.high for b in box_bars)
    box_low = min(b.low for b in box_bars)
    mean_price = sum(b.close for b in box_bars) / len(box_bars)
    if mean_price <= 0:
        return None

    range_pct = (box_high - box_low) / mean_price * 100
    if range_pct > box_range_pct:
        return None

    if last.close <= box_high:
        return None  # no breakout

    box_avg_vol = sum(b.volume for b in box_bars) / len(box_bars)
    if box_avg_vol <= 0:
        return None
    vol_multiplier = last.volume / box_avg_vol
    if vol_multiplier < volume_multiplier_min:
        return None

    return BoxBreakoutResult(
        box_high=box_high,
        box_low=box_low,
        box_days=box_days,
        breakout_price=last.close,
        volume_multiplier=vol_multiplier,
    )


class BoxBreakoutDetector:
    """Stateful — feed daily Bars per symbol, emit on box-breakout.
    Hysteresis: once breakout fires on a given timestamp, no re-emit until
    price returns inside the previous box range."""

    def __init__(
        self,
        bus: EventBus,
        *,
        box_days: int = 10,
        box_range_pct: float = 4.0,
        volume_multiplier_min: float = 2.0,
        publish: Callable[[BoxBreakoutDetected], None] | None = None,
    ) -> None:
        self._bus = bus
        self.box_days = box_days
        self.box_range_pct = box_range_pct
        self.volume_multiplier_min = volume_multiplier_min
        self._publish = publish or (lambda ev: bus.publish(ev))
        self._bars: dict[str, list[Bar]] = {}
        self._last_emitted_box_high: dict[str, int] = {}
        self.emit_count = 0

    def feed(self, bar: Bar) -> None:
        bars = self._bars.setdefault(bar.symbol, [])
        bars.append(bar)
        cap = 3 * self.box_days
        if len(bars) > cap:
            self._bars[bar.symbol] = bars[-cap:]
        result = detect_box_breakout(
            bars,
            box_days=self.box_days,
            box_range_pct=self.box_range_pct,
            volume_multiplier_min=self.volume_multiplier_min,
        )
        if result is None:
            # Reset hysteresis once price re-enters box
            prev_high = self._last_emitted_box_high.get(bar.symbol)
            if prev_high is not None and bar.close <= prev_high:
                del self._last_emitted_box_high[bar.symbol]
            return
        if self._last_emitted_box_high.get(bar.symbol) == result.box_high:
            return  # same box already fired
        self._publish(
            BoxBreakoutDetected(
                symbol=bar.symbol,
                timestamp=bar.timestamp,
                box_high=result.box_high,
                box_low=result.box_low,
                box_days=result.box_days,
                breakout_price=result.breakout_price,
                volume_multiplier=result.volume_multiplier,
            )
        )
        self._last_emitted_box_high[bar.symbol] = result.box_high
        self.emit_count += 1
