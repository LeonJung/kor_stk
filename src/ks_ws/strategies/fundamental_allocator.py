"""FundamentalAllocator — extends Allocator with macro_score input per symbol.

Applies fundamental_strategy.md §3 patterns:
- Pattern 1 Universe Filter — BUY signals on symbols with macro_score below
  ``min_score`` are dropped entirely.
- Pattern 2 Confidence Boost — buy-side aggregate is multiplied by macro_score
  (score > 1.0 amplifies, < 1.0 attenuates).
- Pattern 4 Position Sizing — BUY quantity is further scaled by ``min(score, 1.0)``
  so that weak-macro symbols (but still above min_score) get smaller positions.

SELL signals (exits) are always allowed regardless of macro_score — a position
already opened must be free to close.

macro_score values are expected to live in ~[0.0, 1.5]:
- 0.0 → strong negative (foreign net sell + market regime bear); BUY blocked.
- 1.0 → neutral; BUY behaves identically to plain Allocator.
- 1.5 → strong positive (foreign net buy multiple days + risk-on regime);
        BUY amplified up to the 1.0 magnitude cap.

External feeders (e.g. ForeignNetBuy events, RVOL, regime detectors) drive the
score via ``set_macro_score()``. A helper ``score_from_foreign_flow_krw()``
converts a single foreign net-buy KRW figure into a score for the simplest
fundamental-1 wiring; richer scores combine multiple inputs upstream.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from ks_ws.domain import OrderIntent, Side, Signal
from ks_ws.strategies.allocator import Allocator

_EPSILON = 1e-9


class FundamentalAllocator(Allocator):
    name = "fundamental_allocator"

    def __init__(
        self,
        *,
        max_position_per_symbol: int = 100,
        min_score: float = 0.5,
        default_score: float = 1.0,
    ) -> None:
        super().__init__(max_position_per_symbol=max_position_per_symbol)
        if min_score < 0:
            raise ValueError("min_score must be non-negative")
        if default_score < 0:
            raise ValueError("default_score must be non-negative")
        self.min_score = min_score
        self.default_score = default_score
        self._scores: dict[str, float] = {}

    def set_macro_score(self, symbol: str, score: float) -> None:
        if score < 0:
            raise ValueError("score must be non-negative")
        self._scores[symbol] = score

    def score_for(self, symbol: str) -> float:
        return self._scores.get(symbol, self.default_score)

    def combine(self, signals: list[Signal]) -> list[OrderIntent]:
        if not signals:
            return []

        by_symbol: dict[str, list[Signal]] = defaultdict(list)
        for s in signals:
            by_symbol[s.symbol].append(s)

        # 사용자 룰 (2026-05-15) — Allocator timestamp bug fix:
        # backtest 환경에서 OrderIntent.timestamp 가 datetime.now() 로
        # 덮어쓰여 모든 trade 가 실행 시각으로 몰리는 버그. signal.timestamp
        # 자체가 backtest 에선 historical, live 에선 실제 시각이라 그대로 사용.
        now = max(s.timestamp for s in signals)
        intents: list[OrderIntent] = []
        for symbol, sigs in by_symbol.items():
            macro = self.score_for(symbol)
            buy_sigs = [s for s in sigs if s.side == Side.BUY]
            sell_sigs = [s for s in sigs if s.side == Side.SELL]

            buy_score = sum(self.weight_for(s.strategy) * s.confidence for s in buy_sigs)
            sell_score = sum(self.weight_for(s.strategy) * s.confidence for s in sell_sigs)

            # Pattern 1 + 3 Entry Veto: weak macro fully blocks BUY aggregate.
            if macro < self.min_score:
                buy_score = 0.0

            # Pattern 2 Confidence Boost: BUY side scales by macro.
            buy_score *= macro

            net = buy_score - sell_score
            if abs(net) < _EPSILON:
                continue

            side = Side.BUY if net > 0 else Side.SELL
            magnitude = min(abs(net), 1.0)

            # Pattern 4 Position Sizing: BUY quantity further scales by min(macro, 1.0).
            # SELL keeps full magnitude — exits must not be size-attenuated.
            if side == Side.BUY:
                magnitude *= min(macro, 1.0)

            quantity = max(1, int(magnitude * self.max_position_per_symbol))
            sources = tuple(sorted({s.strategy for s in sigs}))

            intents.append(
                OrderIntent(
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    timestamp=now,
                    sources=sources,
                )
            )
        return intents


def score_from_foreign_flow_krw(
    net_krw: int,
    *,
    strong_threshold_krw: int = 1_000_000_000,
) -> float:
    """Map a single foreign-net-buy KRW figure to a macro_score in [0.0, 1.5].

    - net_krw >= +strong_threshold_krw → 1.5
    - net_krw == 0                    → 1.0
    - net_krw <= -strong_threshold_krw → 0.0
    Linear interpolation between.

    For richer scoring combine with RVOL, regime, gap predictor upstream and
    call ``FundamentalAllocator.set_macro_score()`` with the blended result.
    """
    if strong_threshold_krw <= 0:
        raise ValueError("strong_threshold_krw must be positive")
    if net_krw >= strong_threshold_krw:
        return 1.5
    if net_krw <= -strong_threshold_krw:
        return 0.0
    if net_krw >= 0:
        return 1.0 + 0.5 * (net_krw / strong_threshold_krw)
    return 1.0 + (net_krw / strong_threshold_krw)
