"""Wilson score confidence interval for binomial proportions (win rate).

memory `feedback_strategy_validation_priority`:
- n 작으면 win_rate 신뢰 X
- 예: 5 trades 4 win = 80% 같지만 95% CI = [38%, 96%] = 의미 거의 X
- 200 trades 120 win = 60% / CI = [53%, 67%] = 통계적 의미

Wilson score interval (more accurate than normal approximation for small n):
    p̂ = wins / n
    z = 1.96 (95% CI)
    denom = 1 + z² / n
    center = (p̂ + z² / (2n)) / denom
    half = z * sqrt((p̂(1-p̂) + z² / (4n)) / n) / denom
    CI = (center - half, center + half)
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_Z_95 = 1.96
_Z_99 = 2.576


@dataclass(frozen=True)
class WinRateCI:
    n: int
    wins: int
    point_estimate: float  # wins / n
    lower: float
    upper: float
    confidence: float  # 0.95 / 0.99 etc

    @property
    def width(self) -> float:
        return self.upper - self.lower

    def is_significantly_above(self, threshold: float) -> bool:
        """Lower bound > threshold = win_rate 가 threshold 보다 명백히 위."""
        return self.lower > threshold


def wilson_ci(wins: int, n: int, confidence: float = 0.95) -> WinRateCI:
    if n <= 0:
        raise ValueError("n must be positive")
    if wins < 0 or wins > n:
        raise ValueError("wins must be in [0, n]")
    if confidence == 0.99:
        z = _Z_99
    elif confidence == 0.95:
        z = _Z_95
    else:
        raise ValueError("confidence must be 0.95 or 0.99")

    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return WinRateCI(
        n=n, wins=wins, point_estimate=p,
        lower=max(0.0, center - half),
        upper=min(1.0, center + half),
        confidence=confidence,
    )


def is_better_than_random(wins: int, n: int, confidence: float = 0.95) -> bool:
    """Win rate 의 95% CI lower bound > 50% 인지. = random 보다 의미 있게 위."""
    ci = wilson_ci(wins, n, confidence)
    return ci.is_significantly_above(0.5)
