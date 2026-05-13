"""CupHandleDetector — 컵 앤 핸들 (technical §J12).

alphasquare + threads (joo____wol) 정의:
"1차 상승 → U자 조정 (고점 -30% 이내) → handle 형성 (작은 조정) →
handle 상단 저항 돌파 → 2차 상승. strict criteria 시 ~80% 성공률."

V1 단순 detection:
- cup_bottom_idx = lookback window 안 가장 깊은 봉
- left_rim_idx = cup_bottom 이전 max high (좌측 림)
- right_rim_idx = cup_bottom 이후 cup 영역 (handle 직전) 의 max high
- 두 rim ±cup_symmetry_pct (default 5%) 안 균형
- depth = (left_rim - cup_bottom) / left_rim ≤ cup_depth_max_pct (default 30%)
- handle = right_rim 이후 (last - handle_days_min ... last-1) 박스 — high < right_rim
- 마지막 봉 close > handle high (breakout)

API:
- detect_cup_handle(bars, lookback=60, cup_depth_max_pct=30.0, ...) → Result | None
- CupHandleDetector — stateful + hysteresis
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ks_ws.bus import EventBus
from ks_ws.domain import Bar
from ks_ws.events import CupHandleDetected


@dataclass
class CupHandleResult:
    cup_left_rim: int
    cup_bottom: int
    cup_right_rim: int
    handle_high: int
    handle_low: int
    breakout_price: int


def detect_cup_handle(
    bars: Sequence[Bar],
    *,
    lookback: int = 60,
    cup_depth_max_pct: float = 30.0,
    cup_symmetry_pct: float = 5.0,
    handle_days_min: int = 2,
    handle_days_max: int = 8,
    min_cup_bars: int = 10,
) -> CupHandleResult | None:
    """Detect a cup-and-handle pattern completed on the last bar."""
    if cup_depth_max_pct <= 0 or cup_symmetry_pct <= 0:
        raise ValueError("percentages must be positive")
    if handle_days_min <= 0 or handle_days_max < handle_days_min:
        raise ValueError("invalid handle_days range")
    if min_cup_bars <= 0:
        raise ValueError("min_cup_bars must be positive")
    min_total = min_cup_bars + handle_days_min + 1
    if len(bars) < min_total:
        return None

    window = list(bars[-lookback:])
    n = len(window)
    if n < min_total:
        return None

    last = window[-1]

    # Try different handle lengths
    for handle_days in range(handle_days_min, handle_days_max + 1):
        # Layout: cup_window (len n - handle_days - 1) | handle (len handle_days) | last (breakout).
        cup_window = window[: n - handle_days - 1]
        if len(cup_window) < min_cup_bars:
            continue

        # cup_bottom = cup_window 안 최저 low (단 edges 가까운 봉 X)
        edge_buffer = 2
        cup_inner = list(range(edge_buffer, len(cup_window) - edge_buffer))
        if not cup_inner:
            continue
        cup_bottom_idx = min(cup_inner, key=lambda i: cup_window[i].low)
        cup_bottom = cup_window[cup_bottom_idx].low

        # left_rim = cup_window[0 : cup_bottom_idx] 안 최고 high
        left_part = cup_window[:cup_bottom_idx]
        if not left_part:
            continue
        left_rim = max(b.high for b in left_part)

        # right_rim = cup_window[cup_bottom_idx + 1 :] 안 최고 high
        right_part = cup_window[cup_bottom_idx + 1 :]
        if not right_part:
            continue
        right_rim = max(b.high for b in right_part)

        # Symmetry check
        sym_tol = max(left_rim, right_rim) * cup_symmetry_pct / 100
        if abs(left_rim - right_rim) > sym_tol:
            continue

        # Cup depth check (cup_bottom must be at least some % below rims, max cap 30%)
        max_rim = max(left_rim, right_rim)
        if max_rim <= 0:
            continue
        depth_pct = (max_rim - cup_bottom) / max_rim * 100
        if depth_pct <= 0 or depth_pct > cup_depth_max_pct:
            continue

        # Handle = the `handle_days` bars immediately before the last (breakout) bar.
        handle = window[-handle_days - 1 : -1]
        if len(handle) != handle_days:
            continue
        handle_high = max(b.high for b in handle)
        handle_low = min(b.low for b in handle)

        # Handle constraints: high < right_rim AND low > cup_bottom (handle inside cup)
        if handle_high >= right_rim:
            continue
        if handle_low <= cup_bottom:
            continue

        # Breakout: last close > handle_high
        if last.close <= handle_high:
            continue

        return CupHandleResult(
            cup_left_rim=left_rim,
            cup_bottom=cup_bottom,
            cup_right_rim=right_rim,
            handle_high=handle_high,
            handle_low=handle_low,
            breakout_price=last.close,
        )
    return None


class CupHandleDetector:
    """Stateful — feed daily Bars, emit CupHandleDetected on handle breakout.
    Hysteresis: 같은 cup_bottom 면 re-emit X."""

    def __init__(
        self,
        bus: EventBus,
        *,
        lookback: int = 60,
        cup_depth_max_pct: float = 30.0,
        cup_symmetry_pct: float = 5.0,
        handle_days_min: int = 2,
        handle_days_max: int = 8,
        min_cup_bars: int = 10,
        publish: Callable[[CupHandleDetected], None] | None = None,
    ) -> None:
        self._bus = bus
        self.lookback = lookback
        self.cup_depth_max_pct = cup_depth_max_pct
        self.cup_symmetry_pct = cup_symmetry_pct
        self.handle_days_min = handle_days_min
        self.handle_days_max = handle_days_max
        self.min_cup_bars = min_cup_bars
        self._publish = publish or (lambda ev: bus.publish(ev))
        self._bars: dict[str, list[Bar]] = {}
        self._last_emitted_cup_bottom: dict[str, int] = {}
        self.emit_count = 0

    def feed(self, bar: Bar) -> None:
        bars = self._bars.setdefault(bar.symbol, [])
        bars.append(bar)
        cap = 2 * self.lookback
        if len(bars) > cap:
            self._bars[bar.symbol] = bars[-cap:]
        result = detect_cup_handle(
            bars,
            lookback=self.lookback,
            cup_depth_max_pct=self.cup_depth_max_pct,
            cup_symmetry_pct=self.cup_symmetry_pct,
            handle_days_min=self.handle_days_min,
            handle_days_max=self.handle_days_max,
            min_cup_bars=self.min_cup_bars,
        )
        if result is None:
            return
        if self._last_emitted_cup_bottom.get(bar.symbol) == result.cup_bottom:
            return
        self._publish(
            CupHandleDetected(
                symbol=bar.symbol,
                timestamp=bar.timestamp,
                cup_left_rim=result.cup_left_rim,
                cup_bottom=result.cup_bottom,
                cup_right_rim=result.cup_right_rim,
                handle_high=result.handle_high,
                handle_low=result.handle_low,
                breakout_price=result.breakout_price,
            )
        )
        self._last_emitted_cup_bottom[bar.symbol] = result.cup_bottom
        self.emit_count += 1
