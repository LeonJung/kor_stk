"""macro_score blender — fundamental 의 multi-input score 결합.

여러 fundamental component (외인 net buy / RVOL / regime / 갭 추정 / 섹터 강도)
가 각각 [0.0, 1.5] 점수를 내면, blend_macro_scores() 가 평균 또는 가중평균으로
하나의 macro_score 산출. FundamentalAllocator.set_macro_score() 에 직접 주입.

설계 원칙:
- 모든 component 가 동일 scale [0.0, 1.5] (1.0 = neutral 기준)
- 가중치 X = 단순 평균. 가중치 O = 가중평균.
- 결과는 [0.0, 1.5] cap.

예시 사용 (fundamental 1 + 2 결합):

    from ks_ws.sources.foreign_flow import kis_foreign_flow_fetcher
    from ks_ws.sources.rvol import compute_rvol_value, score_from_rvol
    from ks_ws.strategies.fundamental_allocator import (
        FundamentalAllocator, score_from_foreign_flow_krw,
    )
    from ks_ws.sources.macro_score import blend_macro_scores

    alloc = FundamentalAllocator(min_score=0.5)
    for symbol in universe:
        foreign_net = kis_foreign_flow_fetcher(symbol)
        rvol = compute_rvol_value(symbol, bar_store, today_value_krw)
        score = blend_macro_scores(
            score_from_foreign_flow_krw(foreign_net),
            score_from_rvol(rvol),
        )
        alloc.set_macro_score(symbol, score)
"""

from __future__ import annotations


def blend_macro_scores(
    *scores: float,
    weights: list[float] | tuple[float, ...] | None = None,
) -> float:
    """Combine N fundamental component scores into one macro_score in [0.0, 1.5].

    - No scores → 1.0 (neutral fallback).
    - weights=None → arithmetic mean.
    - weights=[w1, w2, ...] → weighted mean. weights must be non-negative,
      length matching, sum > 0.
    """
    if not scores:
        return 1.0
    if weights is None:
        avg = sum(scores) / len(scores)
    else:
        if len(weights) != len(scores):
            raise ValueError(
                f"weights length {len(weights)} != scores length {len(scores)}"
            )
        if any(w < 0 for w in weights):
            raise ValueError("weights must be non-negative")
        wsum = sum(weights)
        if wsum <= 0:
            raise ValueError("sum of weights must be positive")
        avg = sum(s * w for s, w in zip(scores, weights, strict=True)) / wsum
    return max(0.0, min(1.5, avg))
