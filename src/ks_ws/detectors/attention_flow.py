"""AttentionFlowDetector — emit ManiaSignal when a symbol exhibits high
attention (turnover ↑↑ + |change_pct| ↑↑ + news_count ↑↑).

book Sec 22 광기 (mania) 사상: 광기 종목은 수익이 크지만 위험도 크다. 광기
점수 계산 + 임계 이상 시 cooldown 트리거 또는 별도 strategy 활성화.

Score 계산 (0~1):
- turnover_norm = log10(turnover_krw + 1) / log10_cap
- change_norm = min(1.0, abs(change_pct) / change_pct_cap)
- news_norm = min(1.0, news_count / news_cap)
- score = 0.5 × turnover_norm + 0.3 × change_norm + 0.2 × news_norm

caller 가 매 종목 stats (turnover, change_pct, news_count) 주입 → score
threshold 이상이면 emit.
"""

import math
from collections.abc import Callable
from datetime import datetime

from ks_ws.events import ManiaSignal


class AttentionFlowDetector:
    def __init__(
        self,
        *,
        emit: Callable[[ManiaSignal], None],
        score_threshold: float = 0.7,
        turnover_log10_cap: float = 12.0,  # 10^12 KRW = 1조
        change_pct_cap: float = 30.0,  # 상한가 = 30%
        news_count_cap: int = 50,
    ) -> None:
        if not (0.0 <= score_threshold <= 1.0):
            raise ValueError("score_threshold must be in [0, 1]")
        if turnover_log10_cap <= 0 or change_pct_cap <= 0 or news_count_cap < 1:
            raise ValueError("caps must be positive")
        self._emit = emit
        self.score_threshold = score_threshold
        self.turnover_log10_cap = turnover_log10_cap
        self.change_pct_cap = change_pct_cap
        self.news_count_cap = news_count_cap

    def evaluate(
        self,
        *,
        symbol: str,
        turnover_krw: int,
        change_pct: float,
        news_count: int = 0,
        when: datetime,
    ) -> float:
        """Compute and return mania score for this symbol. Emit if ≥ threshold."""
        turnover_norm = math.log10(max(1, turnover_krw)) / self.turnover_log10_cap
        turnover_norm = min(1.0, max(0.0, turnover_norm))
        change_norm = min(1.0, abs(change_pct) / self.change_pct_cap)
        news_norm = min(1.0, news_count / self.news_count_cap)
        score = 0.5 * turnover_norm + 0.3 * change_norm + 0.2 * news_norm
        if score >= self.score_threshold:
            self._emit(
                ManiaSignal(
                    symbol=symbol,
                    timestamp=when,
                    score=score,
                    turnover_krw=turnover_krw,
                    change_pct=change_pct,
                    news_count=news_count,
                )
            )
        return score
