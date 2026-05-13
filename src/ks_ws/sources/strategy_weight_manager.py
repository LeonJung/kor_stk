"""StrategyWeightManager — review_log win_rate 기반 strategy weight 자동 조정.

memory `feedback_performance_report_format` + 사용자 룰 "꾸준한 승률".

목적:
- review_log 의 각 strategy 의 최근 N일 win_rate / pnl 을 분석
- win_rate 낮은 strategy → weight 감소 (또는 0 = 비활성화)
- 회복 (win_rate 다시 상승) 시 weight 자동 회복
- Allocator.set_weight() 호출 → BUY signal aggregation 시 strategy 별 영향 조정

알고리즘:
- 최소 n_min trades 누적된 strategy 만 평가 (default 5)
- win_rate = wins / total
- weight 매핑:
  - win_rate >= upper (default 0.55) → weight 1.2
  - win_rate >= mid (default 0.40) → weight 1.0
  - win_rate >= lower (default 0.25) → weight 0.5
  - win_rate < lower → weight 0.0 (비활성화)
- 신규 strategy (n < n_min) 는 default weight 1.0 유지

장중 변경 X — paper_trade 시작 시 1번 호출 + (option) 매 N 시간마다 갱신.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

log = logging.getLogger("ks_ws.sources.strategy_weight_manager")


class _AllocatorLike(Protocol):
    def set_weight(self, strategy_name: str, weight: float) -> None: ...


@dataclass(frozen=True)
class StrategyWeight:
    strategy: str
    n: int
    wins: int
    losses: int
    win_rate: float
    weight: float
    reason: str  # "high_winrate" / "ok" / "weak" / "disabled" / "insufficient"


def compute_strategy_weights(
    db_path: Path | str,
    *,
    days: int = 14,
    n_min: int = 5,
    upper_winrate: float = 0.55,
    mid_winrate: float = 0.40,
    lower_winrate: float = 0.25,
    upper_weight: float = 1.2,
    mid_weight: float = 1.0,
    lower_weight: float = 0.5,
    disabled_weight: float = 0.0,
    default_weight: float = 1.0,
    now_utc: datetime | None = None,
    strategies: Sequence[str] | None = None,
) -> list[StrategyWeight]:
    """Compute per-strategy weights from review_log within last ``days``.

    - ``strategies``: optional list to ensure all strategies are reported
      (even if 0 trades). Strategies not in this list and with 0 rows
      are skipped.
    """
    if days <= 0 or n_min <= 0:
        raise ValueError("days and n_min must be positive")
    if not (0 < lower_winrate < mid_winrate < upper_winrate < 1):
        raise ValueError("winrates must be 0 < lower < mid < upper < 1")
    db = Path(db_path)
    rows = []
    if db.exists():
        conn = sqlite3.connect(str(db))
        try:
            now = now_utc or datetime.now(UTC)
            since = (now - timedelta(days=days)).isoformat()
            rows = conn.execute(
                "SELECT strategy, "
                "  COUNT(*), "
                "  SUM(CASE WHEN pnl_krw > 0 THEN 1 ELSE 0 END), "
                "  SUM(CASE WHEN pnl_krw < 0 THEN 1 ELSE 0 END) "
                "FROM trade_reviews WHERE exit_ts >= ? GROUP BY strategy",
                (since,),
            ).fetchall()
        finally:
            conn.close()

    seen: set[str] = set()
    out: list[StrategyWeight] = []
    for strategy, n, wins, losses in rows:
        seen.add(strategy)
        n = int(n or 0)
        wins = int(wins or 0)
        losses = int(losses or 0)
        win_rate = wins / n if n > 0 else 0.0
        if n < n_min:
            weight = default_weight
            reason = "insufficient"
        elif win_rate >= upper_winrate:
            weight = upper_weight
            reason = "high_winrate"
        elif win_rate >= mid_winrate:
            weight = mid_weight
            reason = "ok"
        elif win_rate >= lower_winrate:
            weight = lower_weight
            reason = "weak"
        else:
            weight = disabled_weight
            reason = "disabled"
        out.append(StrategyWeight(
            strategy=strategy, n=n, wins=wins, losses=losses,
            win_rate=win_rate, weight=weight, reason=reason,
        ))
    if strategies is not None:
        for s in strategies:
            if s not in seen:
                out.append(StrategyWeight(
                    strategy=s, n=0, wins=0, losses=0,
                    win_rate=0.0, weight=default_weight,
                    reason="insufficient",
                ))
    return out


def apply_weights(allocator: _AllocatorLike, weights: list[StrategyWeight]) -> int:
    """Push weights into the allocator. Returns number set."""
    for w in weights:
        allocator.set_weight(w.strategy, w.weight)
    return len(weights)


class StrategyWeightManager:
    """Bundles compute + apply, optionally callable later (장중 갱신).

    Usage::

        mgr = StrategyWeightManager(allocator, "data/trade_review.sqlite",
                                    strategies=["breakout","wedge",...])
        mgr.refresh()  # paper_trade 시작 시
        # later: mgr.refresh() 다시 (장중 weight 재조정)
    """

    def __init__(
        self,
        allocator: _AllocatorLike,
        db_path: Path | str,
        *,
        strategies: Sequence[str] | None = None,
        days: int = 14,
        n_min: int = 5,
    ) -> None:
        self._allocator = allocator
        self._db = Path(db_path)
        self._strategies = list(strategies) if strategies else None
        self.days = days
        self.n_min = n_min
        self.last_applied: list[StrategyWeight] = []

    def refresh(self, *, now_utc: datetime | None = None) -> list[StrategyWeight]:
        weights = compute_strategy_weights(
            self._db, days=self.days, n_min=self.n_min,
            now_utc=now_utc, strategies=self._strategies,
        )
        apply_weights(self._allocator, weights)
        self.last_applied = weights
        log.info(
            "StrategyWeightManager: applied %d weights "
            "(active=%d, weak=%d, disabled=%d, insufficient=%d)",
            len(weights),
            sum(1 for w in weights if w.reason in ("high_winrate", "ok")),
            sum(1 for w in weights if w.reason == "weak"),
            sum(1 for w in weights if w.reason == "disabled"),
            sum(1 for w in weights if w.reason == "insufficient"),
        )
        return weights
