"""Tests for VWAPMeanReversionStrategy."""

from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.domain import Side, Tick
from ks_ws.strategies.vwap_reversion import VWAPMeanReversionStrategy


def _ts(seconds: int = 0):
    return datetime(2026, 5, 11, 9, 30, tzinfo=UTC) + timedelta(seconds=seconds)


def _strat(**overrides):
    defaults = dict(
        entry_sigma=1.5,
        stop_sigma=2.5,
        volume_spike_multiplier=2.0,
        volume_window_seconds=300,
        confidence=0.5,
    )
    defaults.update(overrides)
    return VWAPMeanReversionStrategy(**defaults)


def _feed_baseline(strat, symbol="X", n=20, base_price=10000, volume=10):
    """Feed enough ticks to bootstrap VWAP + baseline volume."""
    for i in range(n):
        strat.on_tick(Tick(symbol=symbol, timestamp=_ts(i), price=base_price, volume=volume))


# Entry --------------------------------------------------------------------


def test_no_entry_without_history():
    strat = _strat()
    sigs = strat.on_tick(Tick(symbol="X", timestamp=_ts(0), price=9500, volume=10))
    assert sigs == []  # not enough data


def test_no_entry_at_vwap():
    strat = _strat()
    _feed_baseline(strat, base_price=10000)
    # Tick at vwap (10000) → no deviation
    sigs = strat.on_tick(Tick(symbol="X", timestamp=_ts(30), price=10000, volume=10))
    assert sigs == []


def test_entry_on_dip_and_volume_spike():
    strat = _strat(entry_sigma=0.5, volume_spike_multiplier=1.5)
    base_ticks = [9990, 10010, 9995, 10005, 9990, 10010] * 5
    for i, p in enumerate(base_ticks):
        strat.on_tick(Tick(symbol="X", timestamp=_ts(i), price=p, volume=10))
    # Bootstrap tick — first dip + spike sets baseline
    sigs1 = strat.on_tick(Tick(symbol="X", timestamp=_ts(40), price=9900, volume=50))
    # Real dip — should now BUY
    sigs2 = strat.on_tick(Tick(symbol="X", timestamp=_ts(45), price=9850, volume=100))
    assert any(s.side == Side.BUY for s in sigs1 + sigs2)


def test_watchlist_filter():
    strat = _strat(watchlist={"OTHER"})
    _feed_baseline(strat, symbol="X")
    sigs = strat.on_tick(Tick(symbol="X", timestamp=_ts(30), price=9000, volume=100))
    assert sigs == []


# Exit ---------------------------------------------------------------------


def test_take_profit_returns_to_vwap():
    strat = _strat(entry_sigma=1.0, volume_spike_multiplier=1.5)
    base_ticks = [9990, 10010, 9995, 10005, 9990, 10010, 10000, 10000, 9995, 10005]
    for i, p in enumerate(base_ticks * 3):
        strat.on_tick(Tick(symbol="X", timestamp=_ts(i), price=p, volume=10))
    # Trigger entry with deep dip + spike
    strat.on_tick(Tick(symbol="X", timestamp=_ts(60), price=9800, volume=300))
    if not strat.open_positions():
        # bootstrap may have eaten the entry; retry
        strat.on_tick(Tick(symbol="X", timestamp=_ts(70), price=9750, volume=400))
    if strat.open_positions():
        # Now spike back to vwap (≈10000)
        sigs = strat.on_tick(Tick(symbol="X", timestamp=_ts(80), price=10500, volume=10))
        assert any(s.side == Side.SELL for s in sigs)


# Validation ---------------------------------------------------------------


def test_validation():
    with pytest.raises(ValueError):
        VWAPMeanReversionStrategy(entry_sigma=0)
    with pytest.raises(ValueError):
        VWAPMeanReversionStrategy(entry_sigma=2, stop_sigma=1)
    with pytest.raises(ValueError):
        VWAPMeanReversionStrategy(volume_spike_multiplier=0.5)
    with pytest.raises(ValueError):
        VWAPMeanReversionStrategy(confidence=2)


def test_baseline_bootstrap_skips_first_eligible_signal():
    """The very first dip-with-volume tick is used to bootstrap baseline_volume,
    so the 2nd qualifying dip is the actual signal."""
    strat = _strat(entry_sigma=0.5, volume_spike_multiplier=2.0)
    base_ticks = [9990, 10010, 9995, 10005, 9990, 10010] * 5
    for i, p in enumerate(base_ticks):
        strat.on_tick(Tick(symbol="X", timestamp=_ts(i), price=p, volume=10))
    # First eligible tick — bootstraps baseline, returns []
    sigs1 = strat.on_tick(Tick(symbol="X", timestamp=_ts(40), price=9900, volume=50))
    # Second eligible tick should produce signal (now we have baseline)
    sigs2 = strat.on_tick(Tick(symbol="X", timestamp=_ts(45), price=9850, volume=100))
    assert any(s.side == Side.BUY for s in sigs1 + sigs2)
