"""Tests for RegimeGate (Tier 4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ks_ws.domain import Bar
from ks_ws.sources.regime_gate import RegimeGate


class _FakeStore:
    def __init__(self, bars):
        self._bars = bars

    def read(self, symbol, tf, **kw):
        return list(self._bars) if symbol == "KOSPI" else []


def _bars(closes):
    """closes 순서대로 일봉 생성 (KOSPI)."""
    base = datetime(2025, 1, 1, tzinfo=UTC)
    return [
        Bar(symbol="KOSPI", timestamp=base + timedelta(days=i),
            timeframe="1d", open=c, high=c, low=c, close=c,
            volume=1, value=c)
        for i, c in enumerate(closes)
    ]


def test_no_store_defaults_active():
    g = RegimeGate(bar_store=None)
    assert g.is_active() is True
    snap = g.snapshot()
    assert snap.kospi_trend_up is True
    assert snap.score >= 1


def test_kospi_uptrend_active():
    # ma5 (last 5) > ma20 (all 20) → kospi up
    closes = list(range(100, 120))  # 100..119, monotonic up
    store = _FakeStore(_bars(closes))
    g = RegimeGate(store)
    snap = g.snapshot()
    assert snap.kospi_trend_up is True
    assert snap.active is True


def test_kospi_downtrend_inactive():
    closes = list(range(120, 100, -1))  # 120..101 down
    store = _FakeStore(_bars(closes))
    g = RegimeGate(store)
    snap = g.snapshot()
    assert snap.kospi_trend_up is False
    assert snap.active is False


def test_vkospi_low_boost():
    # KOSPI 추세 down 이지만 VKOSPI 낮음 → min_score=1 일 때 active
    closes = list(range(120, 100, -1))
    store = _FakeStore(_bars(closes))
    g = RegimeGate(store)
    g.set_vkospi(20.0)
    snap = g.snapshot()
    assert snap.vkospi_low is True
    assert snap.active is True  # vkospi_low 가 1점 채움


def test_min_score_3_strict():
    # 모두 충족해야 active
    closes = list(range(100, 120))  # up
    store = _FakeStore(_bars(closes))
    g = RegimeGate(store, min_score=3)
    g.set_vkospi(20.0)
    g.set_nasdaq_prev_close_pct(1.5)
    snap = g.snapshot()
    assert snap.score == 3
    assert snap.active is True

    g.set_vkospi(30.0)  # high → not low
    snap2 = g.snapshot()
    assert snap2.score == 2
    assert snap2.active is False


def test_short_history_defaults_to_uptrend():
    """20봉 미만 시 conservative default = True."""
    closes = [100, 101, 102]  # 3 bars only
    store = _FakeStore(_bars(closes))
    g = RegimeGate(store)
    snap = g.snapshot()
    assert snap.kospi_trend_up is True


def test_reason_string():
    closes = list(range(100, 120))
    store = _FakeStore(_bars(closes))
    g = RegimeGate(store)
    g.set_vkospi(20.0)
    snap = g.snapshot()
    r = snap.reason()
    assert "kospi_up" in r
    assert "vkospi_low" in r
