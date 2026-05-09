"""LossResponseProtocol — 큰 손실 / 연속 손실 시 자동 cooldown / recovery_mode.

book Section 20 + 사용자 결정 (feedback_strategy_decisions.md):
1. 크게 한 방 맞지 않는 게 제일 중요
2. 이미 맞았다면 무리하게 복구 X, 한 달까지 천천히 복구
3. 큰 손실 / 연속 손실 시 비중 축소

state machine:
- ``NORMAL``: full pos size, no restriction
- ``COOLDOWN`` (≤ 1 day): 단일 매매 -X% 또는 connsecutive 손실 N회 발생 시 진입.
  매매 일시 정지. cooldown 종료 후 RECOVERY 진입.
- ``RECOVERY`` (1개월 default): pos_size cap 0.3, 일일 trade 횟수 cap.
  매주 +20% pos size 한도 회복. 회복 완료 시 NORMAL 복귀.

Risk gate 와 결합:
- LossResponseProtocol.advise(now) → Adjustment(pos_scale, max_trades_today, allow_trading)
- Risk.check(intent, ..., advice=Adjustment) → adjusted_intent or None

본 모듈은 stateful — fill 마다 record_fill 호출 + 매 주문 advise 조회.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum

from ks_ws.domain import OrderIntent


class _Phase(Enum):
    NORMAL = "normal"
    COOLDOWN = "cooldown"
    RECOVERY = "recovery"


@dataclass(frozen=True)
class Advice:
    allow_trading: bool
    pos_scale: float  # multiplier on intent.quantity (1.0 = full)
    phase: str
    reason: str = ""


@dataclass
class _Trade:
    timestamp: datetime
    pnl_krw: float


class LossResponseProtocol:
    def __init__(
        self,
        *,
        max_single_loss_krw: int = 5_000_000,
        consecutive_loss_threshold: int = 5,
        cooldown_duration: timedelta = timedelta(hours=24),
        recovery_duration: timedelta = timedelta(days=30),
        recovery_pos_scale_initial: float = 0.3,
        recovery_pos_scale_step: float = 0.2,
        recovery_step_period: timedelta = timedelta(days=7),
        max_recovery_trades_per_day: int = 5,
    ) -> None:
        if max_single_loss_krw <= 0:
            raise ValueError("max_single_loss_krw must be positive")
        if consecutive_loss_threshold < 1:
            raise ValueError("consecutive_loss_threshold must be >= 1")
        if not (0 < recovery_pos_scale_initial <= 1):
            raise ValueError("recovery_pos_scale_initial must be in (0, 1]")
        if not (0 < recovery_pos_scale_step <= 1):
            raise ValueError("recovery_pos_scale_step must be in (0, 1]")
        if max_recovery_trades_per_day < 1:
            raise ValueError("max_recovery_trades_per_day must be >= 1")
        self.max_single_loss_krw = max_single_loss_krw
        self.consecutive_loss_threshold = consecutive_loss_threshold
        self.cooldown_duration = cooldown_duration
        self.recovery_duration = recovery_duration
        self.recovery_pos_scale_initial = recovery_pos_scale_initial
        self.recovery_pos_scale_step = recovery_pos_scale_step
        self.recovery_step_period = recovery_step_period
        self.max_recovery_trades_per_day = max_recovery_trades_per_day

        self._phase = _Phase.NORMAL
        self._cooldown_until: datetime | None = None
        self._recovery_until: datetime | None = None
        self._recovery_started_at: datetime | None = None
        self._consecutive_losses = 0
        self._recent: deque[_Trade] = deque()

    # -- Recording -----------------------------------------------------

    def record_trade(self, *, pnl_krw: float, when: datetime | None = None) -> None:
        when = when or datetime.now(UTC)
        self._recent.append(_Trade(timestamp=when, pnl_krw=pnl_krw))
        # Trim to last 100
        while len(self._recent) > 100:
            self._recent.popleft()

        if pnl_krw < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        # Trigger cooldown on hard breach
        if pnl_krw <= -self.max_single_loss_krw:
            self._enter_cooldown(when, reason=f"single loss {pnl_krw:,.0f}")
        elif self._consecutive_losses >= self.consecutive_loss_threshold:
            self._enter_cooldown(when, reason=f"consecutive losses {self._consecutive_losses}")

    # -- Phase advancement --------------------------------------------

    def advise(self, when: datetime | None = None) -> Advice:
        when = when or datetime.now(UTC)
        self._maybe_advance(when)
        if self._phase == _Phase.NORMAL:
            return Advice(allow_trading=True, pos_scale=1.0, phase="normal")
        if self._phase == _Phase.COOLDOWN:
            return Advice(
                allow_trading=False,
                pos_scale=0.0,
                phase="cooldown",
                reason=f"cooldown until {self._cooldown_until}",
            )
        # RECOVERY
        scale = self._recovery_scale(when)
        trades_today = self._trades_in_window(when, timedelta(days=1))
        if trades_today >= self.max_recovery_trades_per_day:
            return Advice(
                allow_trading=False,
                pos_scale=scale,
                phase="recovery",
                reason=f"daily trade cap reached ({trades_today})",
            )
        return Advice(
            allow_trading=True,
            pos_scale=scale,
            phase="recovery",
            reason=f"recovery; scale={scale:.2f}",
        )

    def apply(self, intent: OrderIntent, when: datetime | None = None) -> OrderIntent | None:
        """Apply phase + scale to an OrderIntent. Returns None if trading
        disallowed; otherwise returns intent (possibly with reduced quantity).
        """
        advice = self.advise(when)
        if not advice.allow_trading:
            return None
        if advice.pos_scale >= 1.0:
            return intent
        scaled_qty = max(1, int(intent.quantity * advice.pos_scale))
        if scaled_qty == intent.quantity:
            return intent
        return intent.model_copy(update={"quantity": scaled_qty})

    # -- Internal -------------------------------------------------------

    def _enter_cooldown(self, when: datetime, *, reason: str) -> None:
        self._phase = _Phase.COOLDOWN
        self._cooldown_until = when + self.cooldown_duration
        self._recovery_until = self._cooldown_until + self.recovery_duration
        self._recovery_started_at = self._cooldown_until
        self._consecutive_losses = 0  # reset for next round

    def _maybe_advance(self, when: datetime) -> None:
        if self._phase == _Phase.COOLDOWN and self._cooldown_until is not None:
            if when >= self._cooldown_until:
                self._phase = _Phase.RECOVERY
        if self._phase == _Phase.RECOVERY and self._recovery_until is not None:
            if when >= self._recovery_until:
                self._phase = _Phase.NORMAL
                self._cooldown_until = None
                self._recovery_until = None
                self._recovery_started_at = None

    def _recovery_scale(self, when: datetime) -> float:
        if self._recovery_started_at is None:
            return 1.0
        elapsed = when - self._recovery_started_at
        weeks = int(elapsed / self.recovery_step_period)
        scale = self.recovery_pos_scale_initial + weeks * self.recovery_pos_scale_step
        return min(1.0, scale)

    def _trades_in_window(self, when: datetime, window: timedelta) -> int:
        cutoff = when - window
        return sum(1 for t in self._recent if t.timestamp >= cutoff)

    @property
    def phase(self) -> str:
        return self._phase.value

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses
