"""GapPredictor — 다음날 갭 추정 (fundamental §D).

fundamental_strategy.md §D 의 갭 / 다음날 방향 예측 model. 종가베팅 보조 +
다음날 시초가 진입 sizing 의 input.

알려진 leading signal:
- D5 외인 야간 net 추정 (NDF + KS200 ADR 추세) — 가장 강
- D6 미국 시장 종가 (S&P/NASDAQ 전일 종가) — 강한 동조성, 특히 tech 종목
- D6a CME 야간 KOSPI 200 선물 — 다음날 시초가 leading
- D2 시간외 단일가 추세
- I3 PPI / I1 CPI 발표 surprise (별도 macro calendar)

V1 simple linear model (사용자 검증 필요):
  predicted_gap_pct = (
      us_market_change_pct * w_us
      + cme_overnight_change_pct * w_cme
      + after_hours_change_pct * w_ah
      + foreign_overnight_score_offset * w_foreign
  ) / sum(weights of present components)

가중치 default: us=0.35, cme=0.30, after_hours=0.20, foreign=0.15.

API:
- predict_gap_pct(*, us_market_change_pct, ...) → float (signed %)
- score_from_predicted_gap(gap_pct) → [0.7, 1.3] macro_score
  - 큰 갭 상승 예상 → score ↑ (closing_bet 종목 boost)
  - 큰 갭 하락 예상 → score ↓ (BUY veto)
"""

from __future__ import annotations

# Default weights — fundamental_strategy.md §D 의 leading 강도 추정.
_DEFAULT_WEIGHTS = {
    "us": 0.35,
    "cme": 0.30,
    "after_hours": 0.20,
    "foreign": 0.15,
}


def predict_gap_pct(
    *,
    us_market_change_pct: float | None = None,
    cme_overnight_change_pct: float | None = None,
    after_hours_change_pct: float | None = None,
    foreign_overnight_score_offset: float | None = None,
    weights: dict[str, float] | None = None,
) -> float:
    """Weighted-sum prediction of next-day open gap percentage.

    Components (all signed %; None to skip):
    - us_market_change_pct: 어제 S&P 또는 NASDAQ 종가 change % (tech 종목 가까운 동조).
    - cme_overnight_change_pct: CME 야간 KOSPI 200 선물 change %.
    - after_hours_change_pct: 시간외 단일가 change vs 정규장 종가.
    - foreign_overnight_score_offset: 외인 야간 score offset (1.0 neutral 기준 ±,
      예: score 1.3 - 1.0 = +0.3 → +0.3% 갭 기여).

    Returns 0.0 if all components None.
    """
    w = weights if weights is not None else _DEFAULT_WEIGHTS
    if any(v < 0 for v in w.values()):
        raise ValueError("weights must be non-negative")

    components = []
    if us_market_change_pct is not None:
        components.append((us_market_change_pct, w.get("us", 0.0)))
    if cme_overnight_change_pct is not None:
        components.append((cme_overnight_change_pct, w.get("cme", 0.0)))
    if after_hours_change_pct is not None:
        components.append((after_hours_change_pct, w.get("after_hours", 0.0)))
    if foreign_overnight_score_offset is not None:
        components.append((foreign_overnight_score_offset, w.get("foreign", 0.0)))

    if not components:
        return 0.0
    wsum = sum(weight for _, weight in components)
    if wsum <= 0:
        return 0.0
    return sum(value * weight for value, weight in components) / wsum


def score_from_predicted_gap(gap_pct: float, *, strong_gap_pct: float = 2.0) -> float:
    """Map predicted gap → macro_score [0.7, 1.3].

    Anchors (default strong_gap_pct=2.0):
    - gap >= +2% → 1.3 (boost — closing_bet 강 / 다음날 시초 매수)
    - gap == 0   → 1.0 (neutral)
    - gap <= -2% → 0.7 (veto support — closing_bet 차단 / 시초 약세)
    Linear interpolation.
    """
    if strong_gap_pct <= 0:
        raise ValueError("strong_gap_pct must be positive")
    if gap_pct >= strong_gap_pct:
        return 1.3
    if gap_pct <= -strong_gap_pct:
        return 0.7
    if gap_pct >= 0:
        return 1.0 + 0.3 * (gap_pct / strong_gap_pct)
    return 1.0 + 0.3 * (gap_pct / strong_gap_pct)  # same formula symmetric
