"""MarketRegime v3 — score [0.0, 1.5] for FundamentalAllocator integration.

fundamental_strategy.md §E (시장 컨텍스트) + Pattern 7 (regime-based activation)
보강. trend + VKOSPI + 시장 거래대금 결합.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.detectors.regime import (
    MarketRegimeV3,
    _market_value_score,
    _vkospi_score,
    compute_regime_score,
)
from ks_ws.domain import Bar


def _bars(prices: list[float], *, symbol: str = "KOSPI", timeframe: str = "1d") -> list[Bar]:
    """Build a Bar list from a sequence of closes. timestamps spaced 1 day."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    out = []
    for i, p in enumerate(prices):
        out.append(Bar(
            symbol=symbol, timestamp=start + timedelta(days=i), timeframe=timeframe,
            open=p, high=p, low=p, close=p, volume=100, value=100 * int(p),
        ))
    return out


# --- _vkospi_score thresholds ---


def test_vkospi_low_risk_on() -> None:
    assert _vkospi_score(10) == 1.4
    assert _vkospi_score(14.9) == 1.4


def test_vkospi_mid_neutral() -> None:
    assert _vkospi_score(15) == 1.2
    assert _vkospi_score(19.9) == 1.2
    assert _vkospi_score(20) == 1.0


def test_vkospi_high_risk_off() -> None:
    assert _vkospi_score(25) == 0.7
    assert _vkospi_score(29.9) == 0.7
    assert _vkospi_score(30) == 0.3
    assert _vkospi_score(50) == 0.3


# --- _market_value_score thresholds ---


def test_market_value_strong() -> None:
    assert _market_value_score(15_000_000_000_000) == 1.3
    assert _market_value_score(12_000_000_000_000) == 1.3


def test_market_value_neutral() -> None:
    assert _market_value_score(8_000_000_000_000) == 1.1
    assert _market_value_score(5_000_000_000_000) == 1.0


def test_market_value_weak() -> None:
    assert _market_value_score(4_900_000_000_000) == 0.6
    assert _market_value_score(0) == 0.6


# --- compute_regime_score ---


def test_score_trend_only_no_vkospi_no_market() -> None:
    """Bars 만 있어 trend 만 active. uptrend 가격 series → 1.2."""
    prices = [3000 + i * 5 for i in range(60)]  # rising
    bars = _bars(prices)
    score = compute_regime_score(bars)
    # short_window 20: last 20 의 short_change = 약 +1.6%, distance_from_avg > 0
    # → uptrend → 1.2 (trend only, weight 1)
    assert score == 1.2


def test_score_strong_uptrend_alone() -> None:
    """short_window 안 +10% 초과 + 60일 평균 위 → strong_uptrend."""
    prices = [3000] * 40 + [3000 + i * 35 for i in range(20)]  # last 20: +700/3000 ~ +23%
    bars = _bars(prices)
    score = compute_regime_score(bars)
    assert score == 1.5


def test_score_downtrend_alone() -> None:
    """평균 아래 + short -5% 이하 → downtrend → 0.5."""
    prices = [3500 - i * 10 for i in range(60)]  # rapid decline
    bars = _bars(prices)
    score = compute_regime_score(bars)
    assert score == 0.5


def test_score_unknown_when_too_few_bars() -> None:
    """60 보다 적은 bars → unknown → 1.0."""
    bars = _bars([3000] * 30)
    score = compute_regime_score(bars)
    assert score == 1.0


def test_score_combined_uptrend_low_vix_strong_market() -> None:
    """모든 component 강 — uptrend(1.2) + VKOSPI 12(1.4) + 거래대금 15조(1.3).
    Weighted (0.4, 0.3, 0.3) → 0.4*1.2 + 0.3*1.4 + 0.3*1.3 = 0.48 + 0.42 + 0.39 = 1.29.
    """
    prices = [3000 + i * 5 for i in range(60)]
    bars = _bars(prices)
    score = compute_regime_score(
        bars, vkospi=12.0, market_value_krw=15_000_000_000_000
    )
    assert score == pytest.approx(1.29)


def test_score_downtrend_high_vix_weak_market() -> None:
    """downtrend(0.5) + VKOSPI 32(0.3) + 거래대금 4조(0.6).
    0.4*0.5 + 0.3*0.3 + 0.3*0.6 = 0.2 + 0.09 + 0.18 = 0.47.
    """
    prices = [3500 - i * 10 for i in range(60)]
    bars = _bars(prices)
    score = compute_regime_score(
        bars, vkospi=32.0, market_value_krw=4_000_000_000_000
    )
    assert score == pytest.approx(0.47)


def test_score_invalid_vkospi() -> None:
    bars = _bars([3000] * 60)
    with pytest.raises(ValueError):
        compute_regime_score(bars, vkospi=-1.0)


def test_score_invalid_market_value() -> None:
    bars = _bars([3000] * 60)
    with pytest.raises(ValueError):
        compute_regime_score(bars, market_value_krw=-100)


# --- MarketRegimeV3 stateful ---


def test_v3_stateful_score() -> None:
    rv3 = MarketRegimeV3()
    for p in [3000 + i * 5 for i in range(60)]:
        rv3.feed_bar(Bar(
            symbol="KOSPI", timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            timeframe="1d", open=p, high=p, low=p, close=p, volume=100, value=100 * p,
        ))
    rv3.set_vkospi(12.0)
    rv3.set_market_value_krw(15_000_000_000_000)
    assert rv3.score() == pytest.approx(1.29)


def test_v3_score_with_only_bars_set_no_vkospi() -> None:
    rv3 = MarketRegimeV3()
    for p in [3000 + i * 5 for i in range(60)]:
        rv3.feed_bar(Bar(
            symbol="KOSPI", timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            timeframe="1d", open=p, high=p, low=p, close=p, volume=100, value=100 * p,
        ))
    # No vkospi or market value — trend only
    assert rv3.score() == 1.2


def test_v3_invalid_long_window() -> None:
    with pytest.raises(ValueError):
        MarketRegimeV3(long_window=10, short_window=20)


def test_v3_invalid_vkospi() -> None:
    rv3 = MarketRegimeV3()
    with pytest.raises(ValueError):
        rv3.set_vkospi(-1.0)


def test_v3_invalid_market_value() -> None:
    rv3 = MarketRegimeV3()
    with pytest.raises(ValueError):
        rv3.set_market_value_krw(-1)
