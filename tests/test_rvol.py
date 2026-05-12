"""RVOL — compute + score helpers."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from ks_ws.sources.rvol import compute_rvol, compute_rvol_value, score_from_rvol


@dataclass
class _StubBar:
    volume: int
    value: int


class _StubBarStore:
    def __init__(self, bars_by_symbol: dict[str, list[_StubBar]]) -> None:
        self._bars = bars_by_symbol

    def read(self, symbol: str, timeframe: str) -> Iterator[_StubBar]:
        yield from self._bars.get(symbol, [])


# --- compute_rvol (volume / shares) ---


def test_rvol_today_above_average() -> None:
    """오늘 거래량이 평균의 3배 → RVOL 3.0."""
    store = _StubBarStore({"005930": [_StubBar(volume=100, value=0) for _ in range(20)]})
    rvol = compute_rvol("005930", store, today_volume=300, lookback_days=20)
    assert rvol == 3.0


def test_rvol_neutral() -> None:
    """평균과 동일 → 1.0."""
    store = _StubBarStore({"005930": [_StubBar(volume=100, value=0) for _ in range(20)]})
    assert compute_rvol("005930", store, today_volume=100, lookback_days=20) == 1.0


def test_rvol_weak() -> None:
    """오늘 평균의 0.3 → 0.3."""
    store = _StubBarStore({"005930": [_StubBar(volume=100, value=0) for _ in range(20)]})
    assert compute_rvol("005930", store, today_volume=30, lookback_days=20) == pytest.approx(0.3)


def test_rvol_no_history_returns_zero() -> None:
    """일봉 history 없으면 0.0."""
    store = _StubBarStore({})
    assert compute_rvol("005930", store, today_volume=100, lookback_days=20) == 0.0


def test_rvol_uses_last_lookback_only() -> None:
    """lookback 20 = 마지막 20일만 average. 그 이전 데이터 무시."""
    # First 10 bars: vol 1, last 20 bars: vol 100 → avg of last 20 = 100
    bars = [_StubBar(volume=1, value=0) for _ in range(10)] + [
        _StubBar(volume=100, value=0) for _ in range(20)
    ]
    store = _StubBarStore({"005930": bars})
    rvol = compute_rvol("005930", store, today_volume=200, lookback_days=20)
    assert rvol == 2.0


def test_rvol_invalid_lookback() -> None:
    store = _StubBarStore({"005930": [_StubBar(volume=100, value=0)]})
    with pytest.raises(ValueError):
        compute_rvol("005930", store, today_volume=100, lookback_days=0)


def test_rvol_invalid_today_volume() -> None:
    store = _StubBarStore({"005930": [_StubBar(volume=100, value=0)]})
    with pytest.raises(ValueError):
        compute_rvol("005930", store, today_volume=-1, lookback_days=20)


def test_rvol_zero_avg_volume() -> None:
    """모든 과거 거래량 0이면 RVOL 0.0 (division-by-zero 방지)."""
    store = _StubBarStore({"005930": [_StubBar(volume=0, value=0) for _ in range(20)]})
    assert compute_rvol("005930", store, today_volume=100, lookback_days=20) == 0.0


# --- compute_rvol_value (KRW) ---


def test_rvol_value_above_average() -> None:
    store = _StubBarStore({
        "005930": [_StubBar(volume=0, value=1_000_000_000) for _ in range(20)]
    })
    rvol = compute_rvol_value("005930", store, today_value_krw=2_500_000_000, lookback_days=20)
    assert rvol == 2.5


def test_rvol_value_invalid_today_value() -> None:
    store = _StubBarStore({"005930": [_StubBar(volume=0, value=1_000_000_000)]})
    with pytest.raises(ValueError):
        compute_rvol_value("005930", store, today_value_krw=-1, lookback_days=20)


# --- score_from_rvol ---


def test_score_strong_at_3x() -> None:
    assert score_from_rvol(3.0) == 1.5
    assert score_from_rvol(5.0) == 1.5


def test_score_neutral_at_1x() -> None:
    assert score_from_rvol(1.0) == 1.0


def test_score_zero_at_zero_vol() -> None:
    assert score_from_rvol(0.0) == 0.0
    assert score_from_rvol(-0.1) == 0.0  # negative treated as 0


def test_score_interpolates_above_neutral() -> None:
    # 1.0 → 1.0, 3.0 → 1.5; rvol 2.0 = halfway → 1.25
    assert score_from_rvol(2.0) == pytest.approx(1.25)


def test_score_interpolates_below_neutral() -> None:
    # rvol 0.5 = halfway between 0.0 and 1.0 → 0.5
    assert score_from_rvol(0.5) == pytest.approx(0.5)
    assert score_from_rvol(0.3) == pytest.approx(0.3)
