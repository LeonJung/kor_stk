"""multi_timeframe_regime — 일봉 + 분봉 + 틱 결합 regime 점수."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.domain import Bar, Tick
from ks_ws.sources.multi_timeframe_regime import (
    compute_multi_regime,
    daily_regime_score,
    minute_momentum_score,
    tick_burst_score,
)


def _bar(close: int, *, days_ago: int = 0, tf: str = "1d", high: int | None = None,
         low: int | None = None) -> Bar:
    base = datetime(2026, 5, 13, 9, 0, tzinfo=UTC)
    ts = (
        base - timedelta(days=days_ago) if tf == "1d"
        else base - timedelta(minutes=days_ago)
    )
    return Bar(
        symbol="KOSPI", timeframe=tf, timestamp=ts,
        open=close, high=high or close + 5, low=low or close - 5,
        close=close, volume=10_000, value=close * 10_000,
    )


def _index_uptrend(n: int = 80) -> list[Bar]:
    # Strong uptrend: 100 → 120 over 80 days
    return [
        _bar(100 + int(i * 0.25), days_ago=n - i - 1)
        for i in range(n)
    ]


def _index_downtrend(n: int = 80) -> list[Bar]:
    return [
        _bar(120 - int(i * 0.25), days_ago=n - i - 1)
        for i in range(n)
    ]


def _index_sideways(n: int = 80) -> list[Bar]:
    return [_bar(100, days_ago=n - i - 1) for i in range(n)]


# --- daily_regime_score ---


def test_daily_regime_uptrend_high_score() -> None:
    regime, score = daily_regime_score(_index_uptrend())
    assert regime in ("strong_uptrend", "uptrend")
    assert score >= 1.1


def test_daily_regime_downtrend_low_score() -> None:
    regime, score = daily_regime_score(_index_downtrend())
    assert regime == "downtrend"
    assert score == 0.7


def test_daily_regime_short_history_unknown() -> None:
    regime, score = daily_regime_score(_index_sideways(n=10))  # < long_window
    assert regime == "unknown"
    assert score == 1.0


# --- minute_momentum_score ---


def test_minute_momentum_strong_up() -> None:
    bars = [_bar(100 + i, days_ago=15 - i, tf="1m") for i in range(15)]
    assert minute_momentum_score(bars, lookback=15, strong_pct=3.0) == pytest.approx(1.3)


def test_minute_momentum_strong_down() -> None:
    bars = [_bar(100 - i, days_ago=15 - i, tf="1m") for i in range(15)]
    assert minute_momentum_score(bars, lookback=15, strong_pct=3.0) == pytest.approx(0.7)


def test_minute_momentum_flat() -> None:
    bars = [_bar(100, days_ago=15 - i, tf="1m") for i in range(15)]
    assert minute_momentum_score(bars) == 1.0


def test_minute_momentum_empty_fallback() -> None:
    assert minute_momentum_score([]) == 1.0
    assert minute_momentum_score([_bar(100, tf="1m")]) == 1.0  # single bar


def test_minute_momentum_invalid_pct() -> None:
    with pytest.raises(ValueError):
        minute_momentum_score([], strong_pct=-1)


# --- tick_burst_score ---


def _tick(volume: int) -> Tick:
    return Tick(symbol="005930", price=100, volume=volume, timestamp=datetime.now(UTC))


def test_tick_burst_strong() -> None:
    ticks = [_tick(300) for _ in range(10)]
    assert tick_burst_score(ticks, avg_tick_volume=100, strong_burst_ratio=3.0) == 1.2


def test_tick_burst_quiet() -> None:
    ticks = [_tick(10) for _ in range(10)]  # ratio = 0.1
    assert tick_burst_score(ticks, avg_tick_volume=100) == 0.85


def test_tick_burst_normal() -> None:
    ticks = [_tick(100) for _ in range(10)]
    assert tick_burst_score(ticks, avg_tick_volume=100) == pytest.approx(1.0)


def test_tick_burst_no_ticks_fallback() -> None:
    assert tick_burst_score([], avg_tick_volume=100) == 1.0


def test_tick_burst_invalid_ratio() -> None:
    with pytest.raises(ValueError):
        tick_burst_score([_tick(100)], avg_tick_volume=100, strong_burst_ratio=1.0)


# --- compute_multi_regime ---


def test_combined_uptrend_strong_momentum_burst() -> None:
    r = compute_multi_regime(
        index_bars=_index_uptrend(),
        minute_bars=[_bar(100 + i, days_ago=15 - i, tf="1m") for i in range(15)],
        recent_ticks=[_tick(300) for _ in range(10)],
        avg_tick_volume=100,
    )
    # daily 1.1-1.3, minute 1.3, tick 1.2 → geomean ~ 1.20
    assert r.combined > 1.15
    assert r.daily_regime in ("strong_uptrend", "uptrend")


def test_combined_downtrend_weak_momentum() -> None:
    r = compute_multi_regime(
        index_bars=_index_downtrend(),
        minute_bars=[_bar(100 - i, days_ago=15 - i, tf="1m") for i in range(15)],
    )
    # daily 0.7, minute 0.7, tick 1.0 → geomean ~ 0.79
    assert r.combined < 0.85
    assert r.daily_regime == "downtrend"


def test_combined_mixed_disagreement() -> None:
    """일봉 uptrend 인데 분봉 하락 → combined 가 1.0 근처."""
    r = compute_multi_regime(
        index_bars=_index_uptrend(),
        minute_bars=[_bar(100 - i, days_ago=15 - i, tf="1m") for i in range(15)],
    )
    # daily 1.1-1.3, minute 0.7 → geomean somewhere mid-range
    assert 0.85 < r.combined < 1.15


def test_combined_no_ticks_default_neutral_tick() -> None:
    r = compute_multi_regime(
        index_bars=_index_sideways(),
        minute_bars=[],
    )
    # daily ~1.0 (sideways), minute 1.0, tick 1.0 → 1.0
    assert r.combined == pytest.approx(1.0)
    assert r.tick_burst_score == 1.0
