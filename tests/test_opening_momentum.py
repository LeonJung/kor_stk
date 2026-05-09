"""Tests for OpeningMomentumStrategy."""

from datetime import time

import pytest

from ks_ws.domain import Side, Tick
from ks_ws.strategies.opening_momentum import OpeningMomentumStrategy, kst_dt


def _strat(**overrides):
    defaults = dict(
        watchlist={"A005930"},
        surge_pct=5.0,
        take_profit_pct=3.0,
        # Tests use 09:00-09:30 entry window so existing tests at 9:05 still pass.
        entry_window_kst=(time(9, 0), time(9, 30)),
        force_exit_kst=time(9, 50),
        confidence=0.6,
    )
    defaults.update(overrides)
    return OpeningMomentumStrategy(**defaults)


def _tick(symbol: str, price: int, hour: int, minute: int, second: int = 0):
    return Tick(
        symbol=symbol, timestamp=kst_dt(2026, 5, 11, hour, minute, second), price=price, volume=10
    )


# Open price capture -------------------------------------------------------


def test_first_tick_captures_open_price_no_signal():
    s = _strat()
    sigs = s.on_tick(_tick("A005930", 10000, 9, 0))
    assert sigs == []
    assert "A005930" in s._meta
    assert s._meta["A005930"].open_price == 10000


def test_non_watchlist_symbols_ignored():
    s = _strat()
    sigs = s.on_tick(_tick("OTHER", 10000, 9, 0))
    assert sigs == []
    assert "OTHER" not in s._meta


# Entry --------------------------------------------------------------------


def test_no_entry_below_surge_threshold():
    s = _strat(surge_pct=5.0)
    s.on_tick(_tick("A005930", 10000, 9, 0))
    sigs = s.on_tick(_tick("A005930", 10400, 9, 5))  # +4%
    assert sigs == []


def test_entry_at_surge_threshold():
    s = _strat(surge_pct=5.0)
    s.on_tick(_tick("A005930", 10000, 9, 0))
    sigs = s.on_tick(_tick("A005930", 10500, 9, 5))  # +5% exact
    assert len(sigs) == 1
    assert sigs[0].side == Side.BUY
    assert sigs[0].confidence == 0.6
    assert "+5.0%" in sigs[0].note


def test_no_double_entry_when_already_open():
    s = _strat()
    s.on_tick(_tick("A005930", 10000, 9, 0))
    s.on_tick(_tick("A005930", 10500, 9, 5))  # entry
    sigs = s.on_tick(_tick("A005930", 10600, 9, 6))  # would be larger surge
    # already open, no new entry; price is between entry and tp so no exit
    assert sigs == []


# Exits --------------------------------------------------------------------


def test_take_profit_exit():
    s = _strat(surge_pct=5.0, take_profit_pct=3.0)
    s.on_tick(_tick("A005930", 10000, 9, 0))
    s.on_tick(_tick("A005930", 10500, 9, 5))  # entry @ 10500
    sigs = s.on_tick(_tick("A005930", 10815, 9, 10))  # +3% from entry
    assert len(sigs) == 1
    assert sigs[0].side == Side.SELL
    assert "take-profit" in sigs[0].note


def test_entry_hit_stop_loss():
    s = _strat()
    s.on_tick(_tick("A005930", 10000, 9, 0))
    s.on_tick(_tick("A005930", 10500, 9, 5))  # entry @ 10500
    sigs = s.on_tick(_tick("A005930", 10500, 9, 6))  # exact entry hit
    assert len(sigs) == 1
    assert sigs[0].side == Side.SELL
    assert sigs[0].urgency == "high"
    assert "entry hit" in sigs[0].note


def test_force_exit_at_kst_950():
    s = _strat(force_exit_kst=time(9, 50))
    s.on_tick(_tick("A005930", 10000, 9, 0))
    s.on_tick(_tick("A005930", 10500, 9, 5))  # entry
    # tick price is between entry and tp; only force-exit can fire
    sigs = s.on_tick(_tick("A005930", 10700, 9, 50))
    assert len(sigs) == 1
    assert "force exit" in sigs[0].note


def test_no_exit_when_holding_in_band():
    s = _strat()
    s.on_tick(_tick("A005930", 10000, 9, 0))
    s.on_tick(_tick("A005930", 10500, 9, 5))  # entry @ 10500
    sigs = s.on_tick(_tick("A005930", 10700, 9, 6))  # +1.9% from entry
    assert sigs == []


# Reset --------------------------------------------------------------------


def test_reset_for_new_session_clears_state():
    s = _strat()
    s.on_tick(_tick("A005930", 10000, 9, 0))
    s.on_tick(_tick("A005930", 10500, 9, 5))
    assert "A005930" in s._meta
    assert s.open_positions()
    s.reset_for_new_session()
    assert s._meta == {}
    assert s.open_positions() == {}


# Validation ---------------------------------------------------------------


def test_rejects_empty_watchlist():
    with pytest.raises(ValueError, match="watchlist"):
        OpeningMomentumStrategy(watchlist=set())


def test_rejects_invalid_pcts():
    with pytest.raises(ValueError):
        OpeningMomentumStrategy(watchlist={"X"}, surge_pct=0)
    with pytest.raises(ValueError):
        OpeningMomentumStrategy(watchlist={"X"}, take_profit_pct=-1)


def test_rejects_invalid_confidence():
    with pytest.raises(ValueError):
        OpeningMomentumStrategy(watchlist={"X"}, confidence=1.1)


def test_rejects_invalid_entry_window():
    with pytest.raises(ValueError, match="entry_window start"):
        OpeningMomentumStrategy(
            watchlist={"X"}, entry_window_kst=(time(9, 30), time(9, 3))
        )


def test_no_entry_outside_entry_window():
    """Surge happens after entry_window closes — no entry but open price still
    captured and exits still managed."""
    s = OpeningMomentumStrategy(
        watchlist={"A005930"},
        surge_pct=5.0,
        entry_window_kst=(time(9, 3), time(9, 25)),
    )
    s.on_tick(_tick("A005930", 10000, 9, 0))  # captures open
    sigs = s.on_tick(_tick("A005930", 10500, 9, 30))  # surge but past window
    assert sigs == []
    assert s.open_positions() == {}
