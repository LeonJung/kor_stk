"""SelfImprovingWeightUpdater — periodically rebalance Allocator weights
based on per-strategy realized PnL.

PRISM-INSIGHT 의 self-improving cycle 흉내 (사용자 D-9 결정 = "Claude 가
매매 결과 회고 → 개선" 의 자동화 보조). 운영 흐름:

1. Ledger 에 trades 누적
2. ``SelfImprovingWeightUpdater.update_weights(allocator)`` 호출
3. ``aggregate_strategy_pnl`` 으로 strategy 별 expectancy/win_rate 계산
4. score = expectancy_krw × stability_factor (=trades/(trades+k_smooth))
5. score 양수 strategy → weight ↑ (cap), 음수 → weight ↓ (floor)
   - rebalance step = ``learning_rate`` × normalized score
6. weight floor/cap 강제 (default [0.0, 2.0])

Smooth factor: 적은 trades 의 strategy 는 신뢰도 낮아 weight 변경 폭 작게.
``k_smooth=10`` 기본 → 5 trades 시 stability=0.33, 30 trades 시 0.75.

본 updater 는 stateless: 매 호출마다 ledger 전체를 읽고 현재 allocator weights
를 읽어 in-place 갱신. Scheduler.daily_at(15, 35, ...) 또는 .every(7d, ...) 식.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ks_ws.storage.ledger import Ledger
from ks_ws.storage.strategy_pnl import StrategyStats, aggregate_strategy_pnl
from ks_ws.strategies.allocator import Allocator


@dataclass(frozen=True)
class WeightChange:
    strategy: str
    old_weight: float
    new_weight: float
    expectancy_krw: float
    trades: int
    stability: float


@dataclass(frozen=True)
class UpdateReport:
    timestamp: datetime
    changes: tuple[WeightChange, ...]

    def summary(self) -> str:
        lines = [f"SelfImprovingWeightUpdater @ {self.timestamp.isoformat()}"]
        for c in self.changes:
            arrow = "→" if c.new_weight != c.old_weight else "="
            lines.append(
                f"  {c.strategy:25s} {c.old_weight:.3f} {arrow} {c.new_weight:.3f}  "
                f"(exp={c.expectancy_krw:>+10,.0f}  trades={c.trades:>3}  "
                f"stab={c.stability:.2f})"
            )
        return "\n".join(lines)


class SelfImprovingWeightUpdater:
    def __init__(
        self,
        *,
        ledger: Ledger,
        learning_rate: float = 0.1,
        weight_floor: float = 0.0,
        weight_cap: float = 2.0,
        k_smooth: float = 10.0,
        normalize_by: float = 50_000.0,
    ) -> None:
        if learning_rate <= 0 or learning_rate > 1:
            raise ValueError("learning_rate must be in (0, 1]")
        if weight_floor < 0 or weight_cap <= weight_floor:
            raise ValueError("weight_cap must exceed weight_floor >= 0")
        if k_smooth <= 0:
            raise ValueError("k_smooth must be positive")
        if normalize_by <= 0:
            raise ValueError("normalize_by must be positive")
        self.ledger = ledger
        self.learning_rate = learning_rate
        self.weight_floor = weight_floor
        self.weight_cap = weight_cap
        self.k_smooth = k_smooth
        self.normalize_by = normalize_by

    def stats(self) -> dict[str, StrategyStats]:
        return aggregate_strategy_pnl(self.ledger)

    def update(self, allocator: Allocator) -> UpdateReport:
        stats = self.stats()
        changes: list[WeightChange] = []
        for strategy, s in stats.items():
            old = allocator.weight_for(strategy)
            stability = s.trades / (s.trades + self.k_smooth)
            normalized = s.expectancy_krw / self.normalize_by  # +1 ~ -1 ish
            delta = self.learning_rate * stability * normalized
            new = max(self.weight_floor, min(self.weight_cap, old + delta))
            allocator.set_weight(strategy, new)
            changes.append(
                WeightChange(
                    strategy=strategy,
                    old_weight=old,
                    new_weight=new,
                    expectancy_krw=s.expectancy_krw,
                    trades=s.trades,
                    stability=stability,
                )
            )
        return UpdateReport(timestamp=datetime.now(UTC), changes=tuple(changes))
