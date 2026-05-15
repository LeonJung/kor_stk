"""Allocator — combine Signals from multiple strategies into OrderIntents.

Default policy: weighted-sum. For each symbol:

    net_score = sum(weight[strategy] * confidence  for buy signals)
              - sum(weight[strategy] * confidence  for sell signals)

If |net_score| > epsilon, an OrderIntent is emitted with side from the
sign and quantity = ceil(min(|net|, 1.0) * max_position_per_symbol).

Strategies that produce contradictory signals on the same symbol
naturally cancel; strategies that agree compound. Weights default to
1.0 for any strategy not explicitly configured.

Sizing this minimally is intentional — real risk management (position
limits, daily loss caps, exposure by sector) lives in the Risk layer
that follows. The Allocator's job is just translating intent strength
into a concrete quantity.
"""

from collections import defaultdict
from datetime import UTC, datetime

from ks_ws.domain import OrderIntent, Side, Signal

_EPSILON = 1e-9


class Allocator:
    def __init__(
        self, *,
        max_position_per_symbol: int = 100,
        symbol_weights=None,  # SymbolWeightMatrix 또는 호출가능 (strategy, symbol)→float
    ) -> None:
        if max_position_per_symbol <= 0:
            raise ValueError("max_position_per_symbol must be positive")
        self.max_position_per_symbol = max_position_per_symbol
        self._weights: dict[str, float] = {}
        self.symbol_weights = symbol_weights

    def set_weight(self, strategy_name: str, weight: float) -> None:
        if weight < 0:
            raise ValueError("weight must be non-negative")
        self._weights[strategy_name] = weight

    def weight_for(self, strategy_name: str) -> float:
        return self._weights.get(strategy_name, 1.0)

    def _symbol_weight(self, strategy: str, symbol: str) -> float:
        """Tier 5 종목별 weight (walk-forward backtest 기반)."""
        if self.symbol_weights is None:
            return 1.0
        try:
            if hasattr(self.symbol_weights, "weight_for"):
                return float(self.symbol_weights.weight_for(strategy, symbol))
            return float(self.symbol_weights(strategy, symbol))
        except Exception:
            return 1.0

    def combine(self, signals: list[Signal]) -> list[OrderIntent]:
        if not signals:
            return []

        by_symbol: dict[str, list[Signal]] = defaultdict(list)
        for s in signals:
            by_symbol[s.symbol].append(s)

        # 사용자 룰 (2026-05-15): backtest 환경에서 OrderIntent.timestamp 가
        # 실행 시각 (datetime.now) 으로 덮어쓰여 모든 trade 가 한 시점 으로
        # 몰리는 버그가 있었음. signal.timestamp 자체가 backtest 에선
        # historical bar 시각, live 에선 실제 시각이라 그대로 쓰면 양쪽 다 정상.
        now = max(s.timestamp for s in signals)
        intents: list[OrderIntent] = []
        for symbol, sigs in by_symbol.items():
            buy_score = sum(
                self.weight_for(s.strategy) * s.confidence for s in sigs if s.side == Side.BUY
            )
            sell_score = sum(
                self.weight_for(s.strategy) * s.confidence for s in sigs if s.side == Side.SELL
            )
            net = buy_score - sell_score
            if abs(net) < _EPSILON:
                continue

            side = Side.BUY if net > 0 else Side.SELL
            magnitude = min(abs(net), 1.0)
            # 사용자 룰 (2026-05-15) Tier 5: BUY 시 종목별 weight 곱.
            # SELL 은 청산이라 weight X 적용 (보유 분 모두 처분).
            sources_set = {s.strategy for s in sigs}
            if side == Side.BUY and self.symbol_weights is not None:
                # 여러 strategy 동시 발화 시 최대 weight 사용 (가장 신뢰 strategy 기준)
                sym_w = max(
                    (self._symbol_weight(strat, symbol) for strat in sources_set),
                    default=1.0,
                )
                magnitude *= sym_w
                if magnitude <= 0:
                    continue  # weight=0 = 차단
            quantity = max(1, int(magnitude * self.max_position_per_symbol))
            sources = tuple(sorted(sources_set))

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
