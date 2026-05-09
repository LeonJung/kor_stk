"""SeedRampUpGuard (게임 부록 ①) — 시드 급증 시 매매단위 cap.

book 게임 부록 ①: 평소 100만원 매매하던 사람이 갑자기 5천만원 매매하면 위험.
시드가 늘어났다고 매매 단위가 비례해서 늘면 안 됨. 점진적 ramp-up.

V1 디자인:
- 매매 단위 (per_trade_max_krw) 를 시드의 N% 로 cap
- 시드가 baseline 대비 ratio 이상 증가하면, 새 매매 단위 = baseline × ramp_per_period
  (ramp_per_period 시간마다 +ramp_step KRW)
- caller 가 update_seed(current_seed_krw) 주기적으로 호출
- caller 가 매 intent 마다 cap_intent(intent, ledger_price_per_share) 호출 →
  intent.quantity * price > per_trade_max_krw 면 quantity 축소

Stateful — last seed snapshot + baseline + ramp progression.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ks_ws.domain import OrderIntent, Side


@dataclass
class _RampState:
    baseline_seed_krw: int
    baseline_per_trade_krw: int
    started_at: datetime


class SeedRampUpGuard:
    def __init__(
        self,
        *,
        baseline_seed_krw: int,
        baseline_per_trade_krw: int,
        ramp_step_krw: int = 1_000_000,
        ramp_period: timedelta = timedelta(days=7),
        seed_jump_factor: float = 1.5,
        per_trade_pct_of_seed: float = 0.05,  # default 5%
    ) -> None:
        if baseline_seed_krw <= 0:
            raise ValueError("baseline_seed_krw must be positive")
        if baseline_per_trade_krw <= 0:
            raise ValueError("baseline_per_trade_krw must be positive")
        if ramp_step_krw <= 0:
            raise ValueError("ramp_step_krw must be positive")
        if ramp_period <= timedelta(0):
            raise ValueError("ramp_period must be positive")
        if seed_jump_factor < 1.0:
            raise ValueError("seed_jump_factor must be >= 1.0")
        if not (0 < per_trade_pct_of_seed <= 1):
            raise ValueError("per_trade_pct_of_seed must be in (0, 1]")
        self.ramp_step_krw = ramp_step_krw
        self.ramp_period = ramp_period
        self.seed_jump_factor = seed_jump_factor
        self.per_trade_pct_of_seed = per_trade_pct_of_seed
        self._state = _RampState(
            baseline_seed_krw=baseline_seed_krw,
            baseline_per_trade_krw=baseline_per_trade_krw,
            started_at=datetime.now(UTC),
        )
        self._current_seed_krw = baseline_seed_krw

    def update_seed(self, current_seed_krw: int, *, when: datetime | None = None) -> None:
        when = when or datetime.now(UTC)
        if current_seed_krw < 0:
            raise ValueError("current_seed_krw must be non-negative")
        # Detect jump → reset ramp baseline (so ramp restarts from new seed)
        if current_seed_krw >= self._state.baseline_seed_krw * self.seed_jump_factor:
            # New baseline = previous seed (not the jumped value), so per-trade
            # stays close to the OLD comfort level until ramp ramps up
            self._state = _RampState(
                baseline_seed_krw=self._state.baseline_seed_krw,
                baseline_per_trade_krw=self._state.baseline_per_trade_krw,
                started_at=when,
            )
        self._current_seed_krw = current_seed_krw

    def per_trade_cap_krw(self, *, when: datetime | None = None) -> int:
        when = when or datetime.now(UTC)
        elapsed = when - self._state.started_at
        steps = max(0, int(elapsed / self.ramp_period))
        ramped = self._state.baseline_per_trade_krw + steps * self.ramp_step_krw
        # Hard cap = per_trade_pct_of_seed × current_seed
        ceiling = int(self._current_seed_krw * self.per_trade_pct_of_seed)
        return min(ramped, ceiling)

    def cap_intent(
        self,
        intent: OrderIntent,
        *,
        price_per_share_krw: int,
        when: datetime | None = None,
    ) -> OrderIntent | None:
        if intent.side != Side.BUY:
            return intent
        if price_per_share_krw <= 0:
            return intent
        cap = self.per_trade_cap_krw(when=when)
        max_qty = cap // price_per_share_krw
        if max_qty <= 0:
            return None
        if intent.quantity > max_qty:
            return intent.model_copy(update={"quantity": max_qty})
        return intent
