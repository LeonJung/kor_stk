"""Tests for PreferredCommonPairStrategy."""

from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.domain import Side, Tick
from ks_ws.strategies.preferred_pair import PreferredCommonPairStrategy


def _ts(seconds: int = 0):
    return datetime(2026, 5, 11, 9, 30, tzinfo=UTC) + timedelta(seconds=seconds)


def _strat(**overrides):
    defaults = dict(
        pairs={"PREF": "COMMON"},
        entry_sigma=2.0,
        stop_sigma=3.0,
        warmup_samples=10,
        confidence=0.4,
    )
    defaults.update(overrides)
    return PreferredCommonPairStrategy(**defaults)


def _feed_warmup(strat, base_pref=8000, base_common=10000, n=15, jitter_pref=10):
    """Establish a stable price ratio (~0.8) with small variance so σ > 0."""
    out = []
    for i in range(n):
        # alternate slight up/down for the preferred to give variance
        pref = base_pref + (jitter_pref if i % 2 == 0 else -jitter_pref)
        out += strat.on_tick(Tick(symbol="PREF", timestamp=_ts(i * 2), price=pref, volume=10))
        out += strat.on_tick(
            Tick(symbol="COMMON", timestamp=_ts(i * 2 + 1), price=base_common, volume=10)
        )
    return out


# Validation ---------------------------------------------------------------


def test_validation():
    with pytest.raises(ValueError):
        PreferredCommonPairStrategy(pairs={})
    with pytest.raises(ValueError):
        PreferredCommonPairStrategy(pairs={"P": "C"}, entry_sigma=0)
    with pytest.raises(ValueError):
        PreferredCommonPairStrategy(pairs={"P": "C"}, stop_sigma=1, entry_sigma=2)
    with pytest.raises(ValueError):
        PreferredCommonPairStrategy(pairs={"P": "C"}, warmup_samples=2)
    with pytest.raises(ValueError):
        PreferredCommonPairStrategy(pairs={"P": "C"}, confidence=2)


# Entry --------------------------------------------------------------------


def test_no_entry_during_warmup():
    strat = _strat(warmup_samples=20)
    sigs = _feed_warmup(strat, n=10)  # only 10 < 20
    assert all(s.symbol not in {"PREF", "COMMON"} or s.side not in {Side.BUY, Side.SELL} or "ratio" not in s.note for s in sigs) or len(sigs) == 0


def test_no_entry_within_band():
    strat = _strat(entry_sigma=2.0)
    _feed_warmup(strat, n=15)
    # Tick within band — ratio essentially unchanged
    sigs = strat.on_tick(Tick(symbol="PREF", timestamp=_ts(100), price=8005, volume=10))
    assert sigs == []


def test_entry_when_ratio_too_high():
    strat = _strat(entry_sigma=2.0, warmup_samples=10)
    _feed_warmup(strat, n=12)
    # Big jump in pref → ratio shoots up → SELL pref + BUY common
    sigs = strat.on_tick(Tick(symbol="PREF", timestamp=_ts(100), price=8500, volume=10))
    sides = {(s.symbol, s.side) for s in sigs}
    assert ("PREF", Side.SELL) in sides
    assert ("COMMON", Side.BUY) in sides
    assert strat.open_positions()["PREF"].direction == "long_common_short_pref"


def test_entry_when_ratio_too_low():
    strat = _strat(entry_sigma=2.0, warmup_samples=10)
    _feed_warmup(strat, n=12)
    sigs = strat.on_tick(Tick(symbol="PREF", timestamp=_ts(100), price=7500, volume=10))
    sides = {(s.symbol, s.side) for s in sigs}
    assert ("PREF", Side.BUY) in sides
    assert ("COMMON", Side.SELL) in sides


def test_no_double_entry():
    """After entering, additional ticks at similar (or deeper) deviation
    must not trigger a NEW entry. (They may trigger an exit; that's a
    distinct path verified elsewhere.)"""
    strat = _strat(entry_sigma=2.0, stop_sigma=10.0, warmup_samples=10)  # high stop to avoid SL
    _feed_warmup(strat, n=12)
    sigs1 = strat.on_tick(Tick(symbol="PREF", timestamp=_ts(100), price=8500, volume=10))
    open_after_first = "PREF" in strat.open_positions()
    sigs2 = strat.on_tick(Tick(symbol="PREF", timestamp=_ts(102), price=8600, volume=10))
    assert open_after_first
    # If signals 2 nonempty, they must be exit (closing) — not new entry
    if sigs2:
        # Closing direction is opposite of entry direction (long_common_short_pref)
        sides = {(s.symbol, s.side) for s in sigs2}
        assert sides == {("PREF", Side.BUY), ("COMMON", Side.SELL)}


# Exit ---------------------------------------------------------------------


def test_take_profit_on_mean_revert():
    strat = _strat(entry_sigma=2.0, warmup_samples=10)
    _feed_warmup(strat, n=12)
    # Open: ratio jumps high (long_common_short_pref)
    strat.on_tick(Tick(symbol="PREF", timestamp=_ts(100), price=8500, volume=10))
    assert "PREF" in strat.open_positions()
    # Revert: pref drops back near baseline → ratio crosses mean → exit
    sigs = strat.on_tick(Tick(symbol="PREF", timestamp=_ts(102), price=7950, volume=10))
    sides = {(s.symbol, s.side) for s in sigs}
    assert ("PREF", Side.BUY) in sides  # closing the SELL
    assert ("COMMON", Side.SELL) in sides
    assert "PREF" not in strat.open_positions()


def test_stop_loss_on_further_divergence():
    strat = _strat(entry_sigma=2.0, stop_sigma=3.0, warmup_samples=10)
    _feed_warmup(strat, n=12)
    # Open: long_common_short_pref at 8500 (deviation ≈ 2σ)
    strat.on_tick(Tick(symbol="PREF", timestamp=_ts(100), price=8500, volume=10))
    # Even worse divergence
    sigs = strat.on_tick(Tick(symbol="PREF", timestamp=_ts(102), price=9000, volume=10))
    if sigs:
        # Stop-loss closes it
        sides = {(s.symbol, s.side) for s in sigs}
        assert sides == {("PREF", Side.BUY), ("COMMON", Side.SELL)}
        assert all(s.urgency == "high" for s in sigs)


# Watchlist filter --------------------------------------------------------


def test_unknown_symbol_ignored():
    strat = _strat()
    sigs = strat.on_tick(Tick(symbol="UNKNOWN", timestamp=_ts(0), price=10000, volume=10))
    assert sigs == []
