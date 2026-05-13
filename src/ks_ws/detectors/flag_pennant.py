"""FlagPennantDetector — 깃발 / 페넌트 (technical §J10-J11).

한국 단타 트레이더 (깃발형 4 확인법, Threads inters_club):
1. 직전 급등/급락 (깃대)
2. 작은 박스권 형성
3. 깃발 기간 짧음 (1-2주)
4. 거래량 낮음

본 모듈은 bullish flag (= 깃대 상승 + 깃발 횡보/약 하락 + 돌파) 만 감지.
페넌트 = 깃발의 변종 (대칭 삼각형) — 같은 룰로 처리 (high/low 수렴 vs 평행).

API:
- detect_flag_breakout(bars, pole_days=5, pole_min_pct=10.0, flag_days_max=10,
                        flag_range_max_pct=5.0) → Result | None
- FlagPennantDetector — stateful, emit FlagPennantDetected event
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ks_ws.bus import EventBus
from ks_ws.domain import Bar
from ks_ws.events import FlagPennantDetected


@dataclass
class FlagBreakoutResult:
    pole_change_pct: float
    flag_high: int
    flag_low: int
    flag_days: int
    breakout_price: int


def detect_flag_breakout(
    bars: Sequence[Bar],
    *,
    pole_days: int = 5,
    pole_min_pct: float = 10.0,
    flag_days_min: int = 3,
    flag_days_max: int = 10,
    flag_range_max_pct: float = 5.0,
    flag_volume_max_ratio: float = 1.0,
) -> FlagBreakoutResult | None:
    """Detect bullish flag/pennant — pole (sharp rise) → flag (small box) →
    breakout above flag-high on last bar.

    Parameters:
    - pole_days: 깃대 일수 (default 5).
    - pole_min_pct: 깃대 최소 상승 % (default 10%).
    - flag_days_min/max: 깃발 일수 범위 (default 3-10).
    - flag_range_max_pct: 깃발 high/low 폭 (default 5%).
    - flag_volume_max_ratio: 깃발 평균 거래량 / 깃대 평균 ≤ ratio (default 1.0).
    """
    if pole_days <= 0 or pole_min_pct <= 0 or flag_range_max_pct <= 0:
        raise ValueError("parameters must be positive")
    if flag_days_min <= 0 or flag_days_max < flag_days_min:
        raise ValueError("invalid flag_days range")
    min_total = pole_days + flag_days_min + 1
    if len(bars) < min_total:
        return None

    # Try each flag length from min to max
    for flag_days in range(flag_days_min, flag_days_max + 1):
        total = pole_days + flag_days + 1
        if len(bars) < total:
            continue
        window = list(bars[-total:])
        pole = window[:pole_days]
        flag = window[pole_days:-1]
        last = window[-1]
        if len(flag) < flag_days_min:
            continue

        # Pole: rise from pole start close to pole end close
        pole_start = pole[0].close
        pole_end = pole[-1].close
        if pole_start <= 0:
            continue
        pole_pct = (pole_end - pole_start) / pole_start * 100
        if pole_pct < pole_min_pct:
            continue

        # Flag: tight range
        flag_high = max(b.high for b in flag)
        flag_low = min(b.low for b in flag)
        flag_mean = sum(b.close for b in flag) / len(flag)
        if flag_mean <= 0:
            continue
        flag_range_pct = (flag_high - flag_low) / flag_mean * 100
        if flag_range_pct > flag_range_max_pct:
            continue

        # Volume: flag avg ≤ pole avg * ratio
        pole_avg_vol = sum(b.volume for b in pole) / len(pole)
        flag_avg_vol = sum(b.volume for b in flag) / len(flag)
        if pole_avg_vol > 0 and flag_avg_vol > pole_avg_vol * flag_volume_max_ratio:
            continue

        # Breakout: last close > flag high
        if last.close <= flag_high:
            continue

        return FlagBreakoutResult(
            pole_change_pct=pole_pct,
            flag_high=flag_high,
            flag_low=flag_low,
            flag_days=flag_days,
            breakout_price=last.close,
        )
    return None


class FlagPennantDetector:
    """Stateful — feed daily Bars, emit FlagPennantDetected on flag breakout.

    Hysteresis: 같은 flag_high level 이면 re-emit X (chatter 방지).
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        pole_days: int = 5,
        pole_min_pct: float = 10.0,
        flag_days_min: int = 3,
        flag_days_max: int = 10,
        flag_range_max_pct: float = 5.0,
        flag_volume_max_ratio: float = 1.0,
        publish: Callable[[FlagPennantDetected], None] | None = None,
    ) -> None:
        self._bus = bus
        self.pole_days = pole_days
        self.pole_min_pct = pole_min_pct
        self.flag_days_min = flag_days_min
        self.flag_days_max = flag_days_max
        self.flag_range_max_pct = flag_range_max_pct
        self.flag_volume_max_ratio = flag_volume_max_ratio
        self._publish = publish or (lambda ev: bus.publish(ev))
        self._bars: dict[str, list[Bar]] = {}
        self._last_emitted_flag_high: dict[str, int] = {}
        self.emit_count = 0

    def feed(self, bar: Bar) -> None:
        bars = self._bars.setdefault(bar.symbol, [])
        bars.append(bar)
        cap = 3 * (self.pole_days + self.flag_days_max)
        if len(bars) > cap:
            self._bars[bar.symbol] = bars[-cap:]
        result = detect_flag_breakout(
            bars,
            pole_days=self.pole_days,
            pole_min_pct=self.pole_min_pct,
            flag_days_min=self.flag_days_min,
            flag_days_max=self.flag_days_max,
            flag_range_max_pct=self.flag_range_max_pct,
            flag_volume_max_ratio=self.flag_volume_max_ratio,
        )
        if result is None:
            return
        if self._last_emitted_flag_high.get(bar.symbol) == result.flag_high:
            return
        self._publish(
            FlagPennantDetected(
                symbol=bar.symbol,
                timestamp=bar.timestamp,
                pole_change_pct=result.pole_change_pct,
                flag_high=result.flag_high,
                flag_low=result.flag_low,
                flag_days=result.flag_days,
                breakout_price=result.breakout_price,
            )
        )
        self._last_emitted_flag_high[bar.symbol] = result.flag_high
        self.emit_count += 1
