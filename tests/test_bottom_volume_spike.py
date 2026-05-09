"""Tests for SixtyDayLowDetector + BottomVolumeSpikeStrategy."""

from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.detectors.sixty_day_low import SixtyDayLowDetector
from ks_ws.domain import Bar, Side, Tick
from ks_ws.events import SixtyDayLow
from ks_ws.strategies.bottom_volume_spike import BottomVolumeSpikeStrategy


def _bar(close: int, volume: int, day_offset: int = 0):
    return Bar(
        symbol="X",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day_offset),
        timeframe="1d",
        open=close,
        high=close + 10,
        low=close - 10,
        close=close,
        volume=volume,
        value=close * volume,
    )


# SixtyDayLowDetector ----------------------------------------------------


def test_detector_emits_when_in_band_with_volume_spike():
    events: list[SixtyDayLow] = []
    det = SixtyDayLowDetector(
        emit=events.append, window_days=20, band_pct=5.0,
        volume_window=3, volume_multiplier=2.0,
    )
    # 17 normal bars with volume 100 — establish baseline
    for i in range(17):
        det.feed_bar(_bar(close=10000 - i * 50, volume=100, day_offset=i))
    # Last 3 bars = near low + spike volume
    for i in range(3):
        det.feed_bar(_bar(close=8200, volume=500, day_offset=17 + i))
    assert len(events) >= 1
    e = events[0]
    assert e.symbol == "X"
    assert e.volume_multiplier >= 2.0


def test_detector_no_emit_without_volume_spike():
    events = []
    det = SixtyDayLowDetector(
        emit=events.append, window_days=20, band_pct=5.0,
        volume_window=3, volume_multiplier=3.0,
    )
    for i in range(20):
        det.feed_bar(_bar(close=10000 - i * 50, volume=100, day_offset=i))
    # close near low but no volume spike
    det.feed_bar(_bar(close=9100, volume=110, day_offset=20))
    assert events == []


def test_detector_hysteresis():
    """After emit, must wait for price to recover above band before re-emitting.

    Uses a longer window so the detector's volume baseline isn't dominated
    by the spike days themselves.
    """
    events = []
    det = SixtyDayLowDetector(
        emit=events.append, window_days=30, band_pct=5.0,
        volume_window=2, volume_multiplier=2.0,
    )
    # 25 normal bars
    for i in range(25):
        det.feed_bar(_bar(close=10000, volume=100, day_offset=i))
    # First dip + spike → emit
    det.feed_bar(_bar(close=9500, volume=500, day_offset=25))
    det.feed_bar(_bar(close=9450, volume=500, day_offset=26))
    assert len(events) == 1
    # 5 recovery bars normalize baseline + price above band
    for i in range(5):
        det.feed_bar(_bar(close=10500, volume=100, day_offset=27 + i))
    # Second dip + spike → emit again (hysteresis reset by recovery)
    det.feed_bar(_bar(close=9500, volume=500, day_offset=32))
    det.feed_bar(_bar(close=9450, volume=500, day_offset=33))
    assert len(events) == 2


def test_detector_validation():
    with pytest.raises(ValueError):
        SixtyDayLowDetector(emit=lambda e: None, window_days=2)
    with pytest.raises(ValueError):
        SixtyDayLowDetector(emit=lambda e: None, band_pct=0)
    with pytest.raises(ValueError):
        SixtyDayLowDetector(emit=lambda e: None, volume_window=1)
    with pytest.raises(ValueError):
        SixtyDayLowDetector(emit=lambda e: None, volume_multiplier=0)


# BottomVolumeSpikeStrategy ----------------------------------------------


def _event(low=8000, current=8200, ts=None):
    return SixtyDayLow(
        symbol="X",
        timestamp=ts or datetime(2026, 5, 11, tzinfo=UTC),
        low_price=low,
        current_price=current,
        band_pct=2.5,
        volume_multiplier=3.0,
    )


def test_entry_on_event():
    s = BottomVolumeSpikeStrategy()
    sigs = s.on_event(_event())
    assert len(sigs) == 1
    assert sigs[0].side == Side.BUY


def test_no_double_entry():
    s = BottomVolumeSpikeStrategy()
    s.on_event(_event())
    sigs = s.on_event(_event())
    assert sigs == []


def test_take_profit():
    s = BottomVolumeSpikeStrategy(take_profit_pct=5.0)
    s.on_event(_event(low=8000, current=8200))
    sigs = s.on_tick(Tick(symbol="X", timestamp=datetime(2026, 5, 12, tzinfo=UTC),
                          price=int(8200 * 1.06), volume=10))  # +6%
    assert len(sigs) == 1
    assert sigs[0].side == Side.SELL


def test_stop_below_low():
    s = BottomVolumeSpikeStrategy(stop_below_low_pct=2.0)
    s.on_event(_event(low=8000, current=8200))
    sigs = s.on_tick(Tick(symbol="X", timestamp=datetime(2026, 5, 12, tzinfo=UTC),
                          price=int(8000 * 0.97), volume=10))  # below low - 2%
    assert len(sigs) == 1
    assert sigs[0].side == Side.SELL
    assert sigs[0].urgency == "high"


def test_no_exit_in_range():
    s = BottomVolumeSpikeStrategy()
    s.on_event(_event(low=8000, current=8200))
    sigs = s.on_tick(Tick(symbol="X", timestamp=datetime(2026, 5, 12, tzinfo=UTC),
                          price=8400, volume=10))  # +2.4%, neither tp nor sl
    assert sigs == []


def test_watchlist_filter():
    s = BottomVolumeSpikeStrategy(watchlist={"OTHER"})
    sigs = s.on_event(_event())
    assert sigs == []


def test_validation():
    with pytest.raises(ValueError):
        BottomVolumeSpikeStrategy(take_profit_pct=0)
    with pytest.raises(ValueError):
        BottomVolumeSpikeStrategy(stop_below_low_pct=-1)
    with pytest.raises(ValueError):
        BottomVolumeSpikeStrategy(confidence=2)
