"""MultiTimeframeRegime — 일봉 / 분봉 / 틱 결합 regime 점수.

기존 `MarketRegimeDetector` 는 일봉 KOSPI/KOSDAQ 만 사용 → 시장 전반 regime
만 봄. 한 종목 매매 결정엔 부족:
- 일봉이 `uptrend` 라도 종목 분봉이 직전 30분 내 -3% 라면 단타 진입 위험
- 일봉 `sideways` 라도 종목 분봉 +5% + 틱 burst 강하면 단타 적합
- 일봉 `downtrend` 인데 종목 분봉 short-term reversal 도 있음

본 모듈 = **3 timeframe** 점수를 곱해서 종목별 regime score [0.0, 1.5] 산출.
fundamental_strategy.md §3 Pattern 7 (Regime Activation) 의 세분화.

3 score:
1. **daily_score** — index regime (KOSPI/KOSDAQ) 일봉 분류 → 매핑
2. **minute_score** — 종목 최근 N 분봉 close vs first close, % change → [0.7, 1.3]
3. **tick_burst_score** — 최근 M 분 tick volume / 평균 → 활동량 burst factor

`compute_multi_regime()` = geometric mean (모두 양호해야 high score). 결과는
DynamicMacroUpdater base x multi_regime 으로 곱해도 되고, 직접
FundamentalAllocator.set_macro_score() 갱신해도 됨.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ks_ws.detectors.regime import classify_regime
from ks_ws.domain import Bar, Tick

# Categorical → score 매핑 (일봉 regime).
_DAILY_REGIME_SCORE = {
    "strong_uptrend": 1.3,
    "uptrend": 1.1,
    "sideways": 1.0,
    "downtrend": 0.7,
    "unknown": 1.0,  # fail-open if insufficient history
}


@dataclass(frozen=True)
class MultiTimeframeRegimeScore:
    daily_score: float
    minute_score: float
    tick_burst_score: float
    combined: float  # [0.0, 1.5]
    daily_regime: str


def _clamp(v: float, lo: float = 0.0, hi: float = 1.5) -> float:
    return max(lo, min(hi, v))


def daily_regime_score(index_bars: Sequence[Bar]) -> tuple[str, float]:
    regime = classify_regime(index_bars)
    return regime, _DAILY_REGIME_SCORE.get(regime, 1.0)


def minute_momentum_score(
    minute_bars: Sequence[Bar],
    *,
    lookback: int = 15,
    strong_pct: float = 3.0,
) -> float:
    """최근 lookback 개 분봉 close 시퀀스 → [0.7, 1.3].

    last/first - 1 = pct change. +strong_pct → 1.3, -strong_pct → 0.7.
    """
    if strong_pct <= 0:
        raise ValueError("strong_pct must be positive")
    if len(minute_bars) < 2:
        return 1.0
    window = list(minute_bars[-lookback:])
    if window[0].close <= 0:
        return 1.0
    pct = (window[-1].close - window[0].close) / window[0].close * 100
    if pct >= strong_pct:
        return 1.3
    if pct <= -strong_pct:
        return 0.7
    return 1.0 + 0.3 * (pct / strong_pct)


def tick_burst_score(
    recent_ticks: Sequence[Tick],
    avg_tick_volume: float,
    *,
    strong_burst_ratio: float = 3.0,
) -> float:
    """최근 ticks 의 평균 volume vs 누적 평균 → [0.85, 1.2].

    Burst = mean(recent_volume) / avg_tick_volume. ≥ strong_burst_ratio → 1.2,
    == 1.0 → 1.0, ≤ 0.3 → 0.85.
    """
    if strong_burst_ratio <= 1.0:
        raise ValueError("strong_burst_ratio must be > 1")
    if avg_tick_volume <= 0 or not recent_ticks:
        return 1.0
    mean_recent = sum(t.volume for t in recent_ticks) / len(recent_ticks)
    ratio = mean_recent / avg_tick_volume
    if ratio >= strong_burst_ratio:
        return 1.2
    if ratio <= 0.3:
        return 0.85
    # Linear: ratio 1.0 → 1.0, 3.0 → 1.2 (so each +1.0 ratio = +0.1)
    return 1.0 + 0.1 * (ratio - 1.0)


def compute_multi_regime(
    *,
    index_bars: Sequence[Bar],
    minute_bars: Sequence[Bar],
    recent_ticks: Sequence[Tick] = (),
    avg_tick_volume: float = 0.0,
) -> MultiTimeframeRegimeScore:
    regime, d_score = daily_regime_score(index_bars)
    m_score = minute_momentum_score(minute_bars)
    t_score = (
        tick_burst_score(recent_ticks, avg_tick_volume)
        if avg_tick_volume > 0 and recent_ticks
        else 1.0
    )
    # Geometric mean — penalize disagreement (one weak score drags combined down).
    combined = (d_score * m_score * t_score) ** (1 / 3)
    return MultiTimeframeRegimeScore(
        daily_score=d_score, minute_score=m_score,
        tick_burst_score=t_score, combined=_clamp(combined),
        daily_regime=regime,
    )
