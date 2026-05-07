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
    def __init__(self, *, max_position_per_symbol: int = 100) -> None:
        if max_position_per_symbol <= 0:
            raise ValueError("max_position_per_symbol must be positive")
        self.max_position_per_symbol = max_position_per_symbol
        self._weights: dict[str, float] = {}

    def set_weight(self, strategy_name: str, weight: float) -> None:
        if weight < 0:
            raise ValueError("weight must be non-negative")
        self._weights[strategy_name] = weight

    def weight_for(self, strategy_name: str) -> float:
        return self._weights.get(strategy_name, 1.0)

    def combine(self, signals: list[Signal]) -> list[OrderIntent]:
        if not signals:
            return []

        by_symbol: dict[str, list[Signal]] = defaultdict(list)
        for s in signals:
            by_symbol[s.symbol].append(s)

        now = datetime.now(UTC)
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
