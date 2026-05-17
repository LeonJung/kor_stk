"""Factor IC (Information Coefficient) measurement.

사용자 룰 (2026-05-15): overfitting 회피 = 시간축 split + Train/Validation/Test
분리. 각 factor 가 forward return 을 예측하는지 cross-sectional Spearman rank
correlation 으로 측정 → 의미 있는 factor 만 합성 점수에 포함.

Definitions:
- Daily IC(t) = Spearman(factor[t], forward_return[t, t+N])
  cross-sectional 종목 간 순위 일치도
- Mean IC = avg(daily IC)
- ICIR (IC Information Ratio) = Mean IC / Std(daily IC)
- Hit rate = % of days with IC > 0
- |Mean IC| > 0.02 + ICIR > 0.5 = significant factor

Test cutoff: 2025-08-01 (Validation 9개월 + Test 9개월).
Train+Val 데이터로만 factor 선별 → Test 는 final 평가 한 번만.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime


# Test data freeze cutoff (사용자 룰 2026-05-15)
TEST_CUTOFF = date(2025, 8, 1)


@dataclass(frozen=True)
class ICResult:
    factor_name: str
    n_days: int
    mean_ic: float
    std_ic: float
    icir: float           # Mean IC / Std IC
    hit_rate: float       # IC > 0 day 비율
    min_ic: float
    max_ic: float
    significant: bool     # |mean_ic| > 0.02 and icir > 0.5

    def __str__(self) -> str:
        sig = "✓" if self.significant else " "
        return (f"{sig} {self.factor_name:30s} "
                f"IC={self.mean_ic:+.4f} ICIR={self.icir:+.2f} "
                f"hit%={self.hit_rate*100:.0f} n={self.n_days}")


def spearman_rank_correlation(xs: list[float], ys: list[float]) -> float:
    """Spearman rank correlation. NaN/None 제거 후 동일 길이 가정.
    Returns 0.0 if insufficient data or zero variance.
    """
    n = len(xs)
    if n < 3 or n != len(ys):
        return 0.0

    def _ranks(vs: list[float]) -> list[float]:
        # average rank for ties
        idx = sorted(range(n), key=lambda i: vs[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vs[idx[j+1]] == vs[idx[i]]:
                j += 1
            avg_rank = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[idx[k]] = avg_rank
            i = j + 1
        return ranks

    rx = _ranks(xs)
    ry = _ranks(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    denx = math.sqrt(sum((r - mx) ** 2 for r in rx))
    deny = math.sqrt(sum((r - my) ** 2 for r in ry))
    if denx <= 0 or deny <= 0:
        return 0.0
    return num / (denx * deny)


def compute_factor_ic(
    factor_name: str,
    *,
    factor_by_day_sym: dict[date, dict[str, float]],
    return_by_day_sym: dict[date, dict[str, float]],
    min_symbols_per_day: int = 30,
    train_only: bool = True,
    test_cutoff: date | None = None,
) -> ICResult:
    """Day-by-day cross-sectional IC 계산.

    factor_by_day_sym: date → {symbol: factor_value}
    return_by_day_sym: date → {symbol: forward_return}
    """
    cutoff = test_cutoff or TEST_CUTOFF
    daily_ics: list[float] = []
    common_days = sorted(set(factor_by_day_sym) & set(return_by_day_sym))
    for d in common_days:
        if train_only and d >= cutoff:
            continue
        f_d = factor_by_day_sym[d]
        r_d = return_by_day_sym[d]
        common = set(f_d) & set(r_d)
        if len(common) < min_symbols_per_day:
            continue
        xs = [f_d[s] for s in common]
        ys = [r_d[s] for s in common]
        ic = spearman_rank_correlation(xs, ys)
        if ic != 0.0:  # skip degenerate (all ties, etc.)
            daily_ics.append(ic)

    n = len(daily_ics)
    if n == 0:
        return ICResult(
            factor_name=factor_name, n_days=0,
            mean_ic=0.0, std_ic=0.0, icir=0.0,
            hit_rate=0.0, min_ic=0.0, max_ic=0.0,
            significant=False,
        )
    mean_ic = sum(daily_ics) / n
    if n > 1:
        var = sum((x - mean_ic) ** 2 for x in daily_ics) / (n - 1)
        std_ic = math.sqrt(var)
    else:
        std_ic = 0.0
    # 완벽한 일관성 (std=0) + mean≠0 = ICIR 무한대 — 큰 값으로 표현
    if std_ic > 0:
        icir = mean_ic / std_ic
    elif mean_ic != 0:
        icir = math.copysign(1e9, mean_ic)
    else:
        icir = 0.0
    hit_rate = sum(1 for ic in daily_ics if ic > 0) / n
    return ICResult(
        factor_name=factor_name,
        n_days=n,
        mean_ic=mean_ic,
        std_ic=std_ic,
        icir=icir,
        hit_rate=hit_rate,
        min_ic=min(daily_ics),
        max_ic=max(daily_ics),
        significant=(abs(mean_ic) > 0.02 and abs(icir) > 0.5),
    )
