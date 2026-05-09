"""DailyWithdrawalRule (Sec 18) — 일일 PnL > 0 시 자동 출금 추적.

book Sec 18: 매일 수익을 출금. 자만 방지 + 시드 한도 cap.

V1 stub: 실 broker transfer 는 안 함 (KIS API 의 출금 endpoint 별도 + 사용자
명시 필요). 본 모듈은 **추적 + 가상 출금 ledger** 만 관리:
- 일일 마감 시 (15:35 KST) realized PnL > 0 → 가상 출금 기록
- 시드 한도 cap 유지: 누적 시드가 max_seed_krw 초과 시 초과분 만큼 가상 출금
- transfer ledger SQLite 별도 테이블 (or main ledger 의 새 테이블)

본 V1 은 in-memory tracker 로 시작, persistence 는 후속.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime


@dataclass(frozen=True)
class WithdrawalEntry:
    settlement_date: date
    pnl_krw: int
    excess_seed_krw: int  # amount above seed cap
    total_withdrawn_krw: int  # pnl + excess
    seed_after_krw: int


@dataclass
class DailyWithdrawalRule:
    """Track withdrawals per trading day. Stateless apart from accumulated
    seed and history — caller invokes ``settle(date, daily_pnl, current_seed)``
    at end of day.

    seed_cap_krw: maximum seed retained in trading account. Anything above
    is treated as withdrawable (in addition to daily PnL > 0).
    """

    seed_cap_krw: int = 100_000_000  # 1 억 default
    history: list[WithdrawalEntry] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.seed_cap_krw <= 0:
            raise ValueError("seed_cap_krw must be positive")

    def settle(
        self,
        *,
        settlement_date: date,
        daily_pnl_krw: int,
        current_seed_krw: int,
    ) -> WithdrawalEntry:
        """Compute today's withdrawal. Returns the recorded entry.
        Convention: if daily_pnl < 0, no PnL withdrawal (loss stays in seed);
        seed cap may still trigger an excess withdrawal if seed > cap."""
        if current_seed_krw < 0:
            raise ValueError("current_seed_krw must be non-negative")
        pnl_withdraw = max(0, daily_pnl_krw)
        seed_after_pnl_withdraw = current_seed_krw - pnl_withdraw
        excess = max(0, seed_after_pnl_withdraw - self.seed_cap_krw)
        seed_after = seed_after_pnl_withdraw - excess
        entry = WithdrawalEntry(
            settlement_date=settlement_date,
            pnl_krw=pnl_withdraw,
            excess_seed_krw=excess,
            total_withdrawn_krw=pnl_withdraw + excess,
            seed_after_krw=seed_after,
        )
        self.history.append(entry)
        return entry

    @property
    def total_withdrawn_krw(self) -> int:
        return sum(e.total_withdrawn_krw for e in self.history)

    @property
    def total_pnl_withdrawn_krw(self) -> int:
        return sum(e.pnl_krw for e in self.history)

    def latest(self) -> WithdrawalEntry | None:
        return self.history[-1] if self.history else None


def kst_today() -> date:
    from zoneinfo import ZoneInfo
    return datetime.now(UTC).astimezone(ZoneInfo("Asia/Seoul")).date()
