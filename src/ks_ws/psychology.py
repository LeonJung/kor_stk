"""PsychologyGuard — 충동 매매 / 감정 매매 detector + cooldown gate.

book Sec 15, 19:
- 직전 손실 후 짧은 시간 내 동일 종목 재진입 = 충동 매매 (복수 매매)
- 매매 빈도 급증 → 감정 매매 위험 → cooldown

V1 detection:
- ``revenge_window``: N분 내 같은 symbol 손실 → 다시 진입 시 BLOCKED
- ``surge_threshold``: 직전 ``surge_window`` 내 trade 횟수 > N → BLOCKED
- block 동안 cooldown timer 만료까지 같은 symbol BUY 불가

본 guard 도 stateful — fill 마다 record_fill, 매 BUY intent 마다 allow().
"""

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ks_ws.domain import OrderIntent, Side


@dataclass
class _SymbolHistory:
    last_loss_at: datetime | None = None
    last_loss_amount: float = 0.0
    fill_times: deque[datetime] = field(default_factory=deque)
    blocked_until: datetime | None = None


@dataclass(frozen=True)
class GuardDecision:
    allow: bool
    reason: str = ""


class PsychologyGuard:
    def __init__(
        self,
        *,
        revenge_window: timedelta = timedelta(minutes=10),
        revenge_block_duration: timedelta = timedelta(minutes=30),
        surge_threshold: int = 10,
        surge_window: timedelta = timedelta(minutes=15),
    ) -> None:
        if revenge_window <= timedelta(0):
            raise ValueError("revenge_window must be positive")
        if revenge_block_duration <= timedelta(0):
            raise ValueError("revenge_block_duration must be positive")
        if surge_threshold < 2:
            raise ValueError("surge_threshold must be >= 2")
        if surge_window <= timedelta(0):
            raise ValueError("surge_window must be positive")
        self.revenge_window = revenge_window
        self.revenge_block_duration = revenge_block_duration
        self.surge_threshold = surge_threshold
        self.surge_window = surge_window
        self._history: dict[str, _SymbolHistory] = defaultdict(_SymbolHistory)
        self._global_fills: deque[datetime] = deque()

    def record_fill(self, *, symbol: str, side: Side, pnl_krw: float, when: datetime | None = None) -> None:
        when = when or datetime.now(UTC)
        h = self._history[symbol]
        h.fill_times.append(when)
        # Trim
        while h.fill_times and h.fill_times[0] < when - self.surge_window * 2:
            h.fill_times.popleft()

        # Track global trade rate
        self._global_fills.append(when)
        while self._global_fills and self._global_fills[0] < when - self.surge_window * 2:
            self._global_fills.popleft()

        if side == Side.SELL and pnl_krw < 0:
            h.last_loss_at = when
            h.last_loss_amount = pnl_krw
            h.blocked_until = when + self.revenge_block_duration

    def check(self, intent: OrderIntent, when: datetime | None = None) -> GuardDecision:
        when = when or intent.timestamp
        if intent.side != Side.BUY:
            return GuardDecision(allow=True)
        h = self._history.get(intent.symbol)
        if h is not None and h.blocked_until is not None and when < h.blocked_until:
            return GuardDecision(
                allow=False,
                reason=(
                    f"revenge block on {intent.symbol} until {h.blocked_until} "
                    f"(last loss {h.last_loss_amount:,.0f})"
                ),
            )
        # Surge: too many fills globally in surge_window
        recent = sum(1 for t in self._global_fills if t >= when - self.surge_window)
        if recent >= self.surge_threshold:
            return GuardDecision(
                allow=False,
                reason=f"trade surge {recent} fills in last {self.surge_window} → cooldown",
            )
        return GuardDecision(allow=True)

    def apply(self, intent: OrderIntent, when: datetime | None = None) -> OrderIntent | None:
        decision = self.check(intent, when)
        return intent if decision.allow else None
