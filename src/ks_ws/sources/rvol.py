"""RVOL (Relative Volume) — fundamental_strategy.md §C2.

거래량/거래대금이 평균 대비 몇 배인지 측정. 격언 "거래량이 가격을 leading
한다" 의 정량화. RVOL >= 3.0 = institutional interest, < 0.5 = weak.

API:
- compute_rvol(symbol, bar_store, today_volume) — 주식수 기반
- compute_rvol_value(symbol, bar_store, today_value_krw) — 거래대금 기반
  (가격 변동의 영향을 받지 않아 잡주에서도 더 안정)
- score_from_rvol(rvol) — RVOL → macro_score [0.0, 1.5]
  FundamentalAllocator.set_macro_score() 의 입력으로 결합.

외인 flow score 와 함께 평균 또는 가중합 하면 fundamental P1/P2/P4 의 multi-input
score 가 완성됨.
"""

from __future__ import annotations

from ks_ws.storage.bars import BarStore


def _avg_recent(
    bar_store: BarStore,
    symbol: str,
    *,
    lookback_days: int,
    attr: str,
    timeframe: str = "1d",
) -> float:
    if lookback_days <= 0:
        raise ValueError("lookback_days must be positive")
    bars = list(bar_store.read(symbol, timeframe))
    if not bars:
        return 0.0
    recent = bars[-lookback_days:]
    if not recent:
        return 0.0
    total = sum(getattr(b, attr) for b in recent)
    return total / len(recent)


def compute_rvol(
    symbol: str,
    bar_store: BarStore,
    today_volume: int,
    *,
    lookback_days: int = 20,
) -> float:
    """Return today_volume / avg(last N daily volumes). 0.0 if no history."""
    if today_volume < 0:
        raise ValueError("today_volume must be non-negative")
    avg = _avg_recent(bar_store, symbol, lookback_days=lookback_days, attr="volume")
    if avg <= 0:
        return 0.0
    return today_volume / avg


def compute_rvol_value(
    symbol: str,
    bar_store: BarStore,
    today_value_krw: int,
    *,
    lookback_days: int = 20,
) -> float:
    """Return today_value_krw / avg(last N daily values). 0.0 if no history.
    Value-based RVOL is less price-distorted than share-count RVOL."""
    if today_value_krw < 0:
        raise ValueError("today_value_krw must be non-negative")
    avg = _avg_recent(bar_store, symbol, lookback_days=lookback_days, attr="value")
    if avg <= 0:
        return 0.0
    return today_value_krw / avg


def score_from_rvol(rvol: float) -> float:
    """Map RVOL to fundamental macro_score in [0.0, 1.5].

    Anchor points:
    - rvol == 0.0 → 0.0  (no volume — block BUY)
    - rvol == 1.0 → 1.0  (today equals average — neutral)
    - rvol >= 3.0 → 1.5  (strong institutional interest — boost BUY)
    Linear interpolation otherwise. Negative inputs treated as 0.
    """
    if rvol <= 0:
        return 0.0
    if rvol >= 3.0:
        return 1.5
    if rvol >= 1.0:
        return 1.0 + 0.5 * ((rvol - 1.0) / 2.0)
    return float(rvol)
