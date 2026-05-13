"""HeadShouldersDetector — H&S 및 역H&S (technical §J6/J6r).

technical_strategy.md §J6/J6r:
- H&S = 상승 추세 끝, 좌어깨-머리-우어깨 + 넥라인 (이중 어깨 저점 연결).
  넥라인 하향 이탈 시 청산. (현물 매수 X)
- 역H&S = 하락 추세 끝, 같은 모양 거꾸로. 넥라인 상향 돌파 시 매수.

V1 detection (역H&S 위주 — BUY signal):
- bars 마지막 lookback (default 60) 일봉.
- head_idx = window 안 최저점 인덱스. head_low = 가장 낮은 봉의 low.
- left_shoulder_idx ∈ [0, head_idx - min_gap): low 최저점 (단 head_low 보다 큼)
- right_shoulder_idx ∈ (head_idx + min_gap, n-1]: low 최저점
- 두 어깨 ±shoulder_tolerance_pct (default 3%) 안 + head_low 보다 큼
- 넥라인 = (좌어깨 ~ 머리 사이 max high + 머리 ~ 우어깨 사이 max high) / 2
- 마지막 봉 close > 넥라인 → 패턴 완성

V1 단순화: 정통 신뢰성 검증 룰 (volume divergence, neckline slope 등) 생략.

API:
- detect_inverse_head_shoulders(bars, ...) — stateless, returns Result | None
- HeadShouldersDetector — stateful, emit HeadShouldersDetected
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ks_ws.bus import EventBus
from ks_ws.domain import Bar
from ks_ws.events import HeadShouldersDetected


@dataclass
class InverseHeadShouldersResult:
    left_shoulder_idx: int
    head_idx: int
    right_shoulder_idx: int
    left_shoulder_price: int
    head_price: int
    right_shoulder_price: int
    neckline_price: int
    target_price: int  # neckline + (neckline - head_low)


def detect_inverse_head_shoulders(
    bars: Sequence[Bar],
    *,
    lookback: int = 60,
    shoulder_tolerance_pct: float = 3.0,
    head_depth_min_pct: float = 1.0,
    min_gap_bars: int = 3,
) -> InverseHeadShouldersResult | None:
    """Detect an inverse H&S completed on the last bar (neckline broken).

    Parameters:
    - shoulder_tolerance_pct: 좌어깨 / 우어깨 low 차이 허용 (default 3%).
    - head_depth_min_pct: 머리 low 가 어깨 low 보다 최소 N% 더 깊어야 (default 1%).
    - min_gap_bars: 어깨-머리 / 머리-어깨 사이 최소 bar 수 (default 3).
    """
    if shoulder_tolerance_pct <= 0 or head_depth_min_pct <= 0:
        raise ValueError("percentages must be positive")
    if min_gap_bars <= 0:
        raise ValueError("min_gap_bars must be positive")
    if len(bars) < min_gap_bars * 2 + 3:
        return None

    window = list(bars[-lookback:])
    n = len(window)
    if n < min_gap_bars * 2 + 3:
        return None

    # head = window 안 최저 low
    head_idx = min(range(n), key=lambda i: window[i].low)
    head_low = window[head_idx].low

    if head_idx < min_gap_bars or head_idx > n - min_gap_bars - 2:
        return None  # head too close to edges

    # Left shoulder: window[0 : head_idx - min_gap_bars] 안 최저 low (단 > head_low)
    left_region = range(0, head_idx - min_gap_bars + 1)
    if not left_region:
        return None
    left_idx = min(left_region, key=lambda i: window[i].low)
    left_low = window[left_idx].low
    if left_low <= head_low:
        return None
    if (left_low - head_low) / head_low * 100 < head_depth_min_pct:
        return None  # head not deep enough vs left shoulder

    # Right shoulder: window[head_idx + min_gap_bars : n-1] 안 최저 low
    right_region = range(head_idx + min_gap_bars, n - 1)
    if not right_region:
        return None
    right_idx = min(right_region, key=lambda i: window[i].low)
    right_low = window[right_idx].low
    if right_low <= head_low:
        return None
    if (right_low - head_low) / head_low * 100 < head_depth_min_pct:
        return None

    # 좌/우 어깨 균형
    tol = max(left_low, right_low) * shoulder_tolerance_pct / 100
    if abs(left_low - right_low) > tol:
        return None

    # Neckline = 좌어깨~머리 사이 max high + 머리~우어깨 사이 max high 의 평균
    between_left = window[left_idx + 1 : head_idx]
    between_right = window[head_idx + 1 : right_idx]
    if not between_left or not between_right:
        return None
    peak_left = max(b.high for b in between_left)
    peak_right = max(b.high for b in between_right)
    neckline = (peak_left + peak_right) // 2
    if neckline <= max(left_low, right_low):
        return None

    # Last bar close > neckline → breakout
    last_close = window[-1].close
    if last_close <= neckline:
        return None

    return InverseHeadShouldersResult(
        left_shoulder_idx=left_idx,
        head_idx=head_idx,
        right_shoulder_idx=right_idx,
        left_shoulder_price=left_low,
        head_price=head_low,
        right_shoulder_price=right_low,
        neckline_price=neckline,
        target_price=neckline + (neckline - head_low),
    )


class HeadShouldersDetector:
    """Stateful — feed Bars, emit HeadShouldersDetected (inverse pattern, BUY signal).

    Hysteresis: 같은 head_idx timestamp 의 패턴은 한 번만 emit.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        lookback: int = 60,
        shoulder_tolerance_pct: float = 3.0,
        head_depth_min_pct: float = 1.0,
        min_gap_bars: int = 3,
        publish: Callable[[HeadShouldersDetected], None] | None = None,
    ) -> None:
        self._bus = bus
        self.lookback = lookback
        self.shoulder_tolerance_pct = shoulder_tolerance_pct
        self.head_depth_min_pct = head_depth_min_pct
        self.min_gap_bars = min_gap_bars
        self._publish = publish or (lambda ev: bus.publish(ev))
        self._bars: dict[str, list[Bar]] = {}
        self._last_emitted_head_ts: dict[str, object] = {}
        self.emit_count = 0

    def feed(self, bar: Bar) -> None:
        bars = self._bars.setdefault(bar.symbol, [])
        bars.append(bar)
        cap = 2 * self.lookback
        if len(bars) > cap:
            self._bars[bar.symbol] = bars[-cap:]
        result = detect_inverse_head_shoulders(
            bars,
            lookback=self.lookback,
            shoulder_tolerance_pct=self.shoulder_tolerance_pct,
            head_depth_min_pct=self.head_depth_min_pct,
            min_gap_bars=self.min_gap_bars,
        )
        if result is None:
            return
        window = bars[-self.lookback :]
        head_ts = window[result.head_idx].timestamp
        if self._last_emitted_head_ts.get(bar.symbol) == head_ts:
            return
        self._publish(
            HeadShouldersDetected(
                symbol=bar.symbol,
                timestamp=bar.timestamp,
                pattern="inverse_head_shoulders",
                left_shoulder_price=result.left_shoulder_price,
                head_price=result.head_price,
                right_shoulder_price=result.right_shoulder_price,
                neckline_price=result.neckline_price,
                target_price=result.target_price,
            )
        )
        self._last_emitted_head_ts[bar.symbol] = head_ts
        self.emit_count += 1
