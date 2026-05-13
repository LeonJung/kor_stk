"""ForeignFlowTrend — N일 (3-5일) 외인 누적 순매수 추세 점수.

기존 ``score_from_foreign_flow_krw`` 는 *단발* 1일 데이터로 score 산출. 단점:
- 어제만 -3.1조 한 case (event-driven sell-off) 와 5일 연속 -1조 (구조적 매도)
  를 구분 못 함.
- 어제 +1.5조 한 case (단발 buying) 와 5일 연속 +0.3조 (steady accumulation)
  도 구분 못 함.

fundamental_strategy.md §3 Pattern 2 (Confidence Boost) 를 더 robust 하게 하기
위한 추가 차원:
- ``sum_n`` = 최근 N일 net 합계 (단발 vs 추세 모두 누적값)
- ``consistency`` = N일 중 net 의 sign 이 같은 day count / N (추세 일관성)
- 두 input → score [0.0, 1.5]

Score 산출:
- consistency * direction (+1 if mostly buy, -1 if mostly sell, 0 if mixed)
- magnitude = min(|sum_n / strong_n_threshold|, 1.0)
- score = 1.0 + 0.5 * direction * magnitude * consistency
- 5일 연속 매수 + sum -2조: direction=-1, consistency=1.0, magnitude=1.0
  → 0.0 (strong sell trend)
- 1일 +3조 + 4일 0: direction=+1, consistency=0.2, magnitude=1.0
  → 1.0 + 0.5*1*1*0.2 = 1.1 (weak single-day spike)
- 5일 +0.5조 each (consistent steady buy): consistency=1.0, magnitude=1.0
  → 1.5 (strong buy trend)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class TrendScore:
    """3-5일 외인/기관 누적 추세 점수."""
    sum_krw: int  # N일 net 합계
    consistency: float  # 0.0 ~ 1.0
    direction: int  # -1 / 0 / +1
    score: float  # 0.0 ~ 1.5


def compute_trend_score(
    daily_net_krw: Sequence[int],
    *,
    strong_n_threshold_krw: int = 2_000_000_000_000,
) -> TrendScore:
    """N일 (보통 3-5일) net 매수 KRW 시퀀스 → trend score.

    - daily_net_krw: 가장 오래된 → 가장 최근 순서. 모두 0 인 경우 score=1.0.
    - strong_n_threshold_krw: N일 누적이 이 값 이상이면 magnitude=1.0 (default
      = 2조, KOSPI 시장 단위 / 종목별 cap top 종목 기준).
    """
    if strong_n_threshold_krw <= 0:
        raise ValueError("strong_n_threshold_krw must be positive")
    if not daily_net_krw:
        return TrendScore(sum_krw=0, consistency=0.0, direction=0, score=1.0)

    total = sum(daily_net_krw)
    n = len(daily_net_krw)
    pos_days = sum(1 for v in daily_net_krw if v > 0)
    neg_days = sum(1 for v in daily_net_krw if v < 0)

    # Direction: majority sign of total + day-count agreement.
    if total > 0 and pos_days >= neg_days:
        direction = 1
    elif total < 0 and neg_days >= pos_days:
        direction = -1
    else:
        direction = 0

    # Consistency: fraction of days agreeing with direction.
    if direction > 0:
        consistency = pos_days / n
    elif direction < 0:
        consistency = neg_days / n
    else:
        consistency = 0.0

    magnitude = min(abs(total) / strong_n_threshold_krw, 1.0)
    score = 1.0 + 0.5 * direction * magnitude * consistency
    # Clamp to [0.0, 1.5]
    if score < 0.0:
        score = 0.0
    elif score > 1.5:
        score = 1.5
    return TrendScore(
        sum_krw=total, consistency=consistency,
        direction=direction, score=score,
    )


def score_from_foreign_trend(
    daily_net_krw: Sequence[int],
    *,
    strong_n_threshold_krw: int = 2_000_000_000_000,
) -> float:
    """Convenience — return just the score for blend_macro_scores feed."""
    return compute_trend_score(
        daily_net_krw, strong_n_threshold_krw=strong_n_threshold_krw,
    ).score
