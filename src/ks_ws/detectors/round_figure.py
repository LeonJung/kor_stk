"""RoundFigurePriceLevelDetector — 호가단위 변경 경계 + 라운드 피겨 자리 식별.

만쥬 책 Section 10 (technical_strategy.md §C, J3):
"라운드 피겨 = 시장 심리 자극 점. 신고가는 시장이 인정한 가격."
호가단위 변경 경계 (2k/5k/20k/50k/200k/500k) 에서 매수/매도 의사결정이
집중됨 → 자리 자체가 신호.

KRX 호가단위 (tick size) 구간:
  ≤ 2,000원     : 1원
  ≤ 5,000원     : 5원
  ≤ 20,000원    : 10원
  ≤ 50,000원    : 50원
  ≤ 200,000원   : 100원
  ≤ 500,000원   : 500원
  > 500,000원   : 1,000원

→ 변경 경계: **2,000 / 5,000 / 20,000 / 50,000 / 200,000 / 500,000 원**.

추가 라운드 피겨 (가격 자리 심리적 자석): 10,000 / 100,000 / 1,000,000 등
10의 거듭제곱 + 5의 거듭제곱. 본 모듈은 두 종류 (tick-boundary + decimal-round)
다 식별 가능.

API:
- tick_size_boundaries() — 6 개 호가단위 변경 경계 list
- decimal_round_figures(min, max) — 10000/100000/...  range 안의 라운드 피겨
- nearest_round_figure(price) — 가장 가까운 boundary or decimal round
- distance_bp(price, target) — basis point 거리 (signed, 1bp = 0.01%)
- is_near_round_figure(price, tolerance_bp=20) — bool (default ±20bp = ±0.2%)
- score_proximity(price, tolerance_bp=20) — [0.9, 1.3], 가까울수록 ↑
- RoundFigureDetector — stateful, tick.feed → RoundFigureReached event emit
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ks_ws.bus import EventBus
from ks_ws.domain import Tick
from ks_ws.events import RoundFigureReached

# KRX 호가단위 (tick-size) 변경 경계 — 책 사상의 핵심 자리.
TICK_SIZE_BOUNDARIES_KRW: tuple[int, ...] = (
    2_000, 5_000, 20_000, 50_000, 200_000, 500_000,
)


def decimal_round_figures(min_krw: int, max_krw: int) -> list[int]:
    """Return decimal round-figure prices (10^k and 5*10^k) in [min, max]."""
    if min_krw < 0 or max_krw < min_krw:
        raise ValueError("invalid range")
    out: set[int] = set()
    # 10^k starting from 10
    power = 10
    while power <= max_krw * 10:
        # add power, 2*power, 5*power 같은 자석 자리
        for mult in (1, 2, 5):
            candidate = power * mult
            if min_krw <= candidate <= max_krw:
                out.add(candidate)
        power *= 10
    return sorted(out)


def all_round_figures(min_krw: int, max_krw: int) -> list[int]:
    """tick-size boundaries + decimal round figures, deduped + sorted."""
    out = set(decimal_round_figures(min_krw, max_krw))
    for b in TICK_SIZE_BOUNDARIES_KRW:
        if min_krw <= b <= max_krw:
            out.add(b)
    return sorted(out)


def nearest_round_figure(price: int) -> int:
    """Closest round-figure level (tick boundary or decimal round) to the price."""
    if price < 0:
        raise ValueError("price must be non-negative")
    # Consider a wide range around the price to be safe
    lower = max(0, price // 10)
    upper = max(price * 2, price + 1_000_000)
    candidates = all_round_figures(lower, upper)
    if not candidates:
        return TICK_SIZE_BOUNDARIES_KRW[0]
    return min(candidates, key=lambda c: abs(c - price))


def distance_bp(price: int, target: int) -> float:
    """Signed distance from target to price in basis points (1bp = 0.01%).
    Positive = price above target. Returns 0 if target is 0."""
    if target <= 0:
        return 0.0
    return (price - target) / target * 10_000


def is_near_round_figure(price: int, *, tolerance_bp: float = 20.0) -> bool:
    """True if price is within ±tolerance_bp (default ±0.2%) of any round figure."""
    if price <= 0:
        return False
    target = nearest_round_figure(price)
    return abs(distance_bp(price, target)) <= tolerance_bp


def score_proximity(price: int, *, tolerance_bp: float = 20.0) -> float:
    """Map proximity to round figure → score [0.9, 1.3].

    - exact match (0bp distance) → 1.3
    - within tolerance_bp → linearly interpolated 1.3 → 1.0
    - beyond tolerance → 0.9 (mild penalty for being in 'no man's land')
    """
    if price <= 0:
        return 1.0
    target = nearest_round_figure(price)
    d = abs(distance_bp(price, target))
    if d == 0:
        return 1.3
    if d <= tolerance_bp:
        # Linear: 0 → 1.3, tolerance_bp → 1.0
        return 1.3 - 0.3 * (d / tolerance_bp)
    return 0.9


@dataclass
class _RoundFigureState:
    last_emitted_target: int | None = None
    last_was_near: bool = False


class RoundFigureDetector:
    """Stateful detector — feed Ticks, emit RoundFigureReached when price
    enters (crosses into) ±tolerance_bp of a round-figure level.

    Hysteresis: only re-emit when price first leaves tolerance (last_was_near
    flag flips False) then re-enters near a different target. Prevents
    chatter when price oscillates around a single round-figure level.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        tolerance_bp: float = 20.0,
        publish: Callable[[RoundFigureReached], None] | None = None,
    ) -> None:
        if tolerance_bp <= 0:
            raise ValueError("tolerance_bp must be positive")
        self._bus = bus
        self.tolerance_bp = tolerance_bp
        self._publish = publish or (lambda ev: bus.publish(ev))
        self._state: dict[str, _RoundFigureState] = {}
        self.emit_count = 0

    def feed(self, tick: Tick) -> None:
        st = self._state.setdefault(tick.symbol, _RoundFigureState())
        target = nearest_round_figure(tick.price)
        d = abs(distance_bp(tick.price, target))
        near_now = d <= self.tolerance_bp

        if near_now and (not st.last_was_near or st.last_emitted_target != target):
            self._publish(
                RoundFigureReached(
                    symbol=tick.symbol,
                    timestamp=tick.timestamp,
                    boundary_price=target,
                    actual_price=tick.price,
                    distance_bp=distance_bp(tick.price, target),
                )
            )
            st.last_emitted_target = target
            self.emit_count += 1

        st.last_was_near = near_now
