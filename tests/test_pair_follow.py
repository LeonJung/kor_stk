"""Tests for PairFollowStrategy (짝꿍 매매)."""

from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.domain import Side, Tick
from ks_ws.events import LimitUpBroken, LimitUpReached
from ks_ws.strategies.pair_follow import PairFollowStrategy


def _ts(seconds: float = 0):
    return datetime(2026, 5, 11, 9, 30, tzinfo=UTC) + timedelta(seconds=seconds)


def _strat(**overrides):
    defaults = dict(
        pairs={"LEADER": "FOLLOW"},
        take_profit_pct=2.5,
        stop_loss_pct=1.5,
        hold_timeout_seconds=300,
        flat_timeout_seconds=60,
        flat_band_pct=0.5,
        confidence=0.7,
    )
    defaults.update(overrides)
    return PairFollowStrategy(**defaults)


def _reach(symbol="LEADER", price=13000, ts=None):
    return LimitUpReached(
        symbol=symbol,
        timestamp=ts or _ts(),
        limit_up_price=price,
        prev_close=10000,
    )


def _broken(symbol="LEADER", price=12500, ts=None):
    return LimitUpBroken(
        symbol=symbol,
        timestamp=ts or _ts(10),
        limit_up_price=13000,
        current_price=price,
    )


def _tick(symbol="FOLLOW", price=10000, seconds=0):
    return Tick(symbol=symbol, timestamp=_ts(seconds), price=price, volume=10)


# Entry --------------------------------------------------------------------


def test_entry_on_leader_limit_up():
    s = _strat()
    sigs = s.on_event(_reach())
    assert len(sigs) == 1
    assert sigs[0].symbol == "FOLLOW"
    assert sigs[0].side == Side.BUY
    assert sigs[0].confidence == 0.7
    assert "LEADER" in sigs[0].note


def test_entry_only_once_while_open():
    s = _strat()
    s.on_event(_reach())
    s.on_tick(_tick(price=10000, seconds=1))  # capture entry price
    sigs = s.on_event(_reach(ts=_ts(5)))
    assert sigs == []  # already in position


def test_unknown_leader_ignored():
    s = _strat()
    sigs = s.on_event(_reach(symbol="OTHER"))
    assert sigs == []


# Take-profit / Stop-loss -------------------------------------------------


def test_take_profit_exit():
    s = _strat()
    s.on_event(_reach())
    s.on_tick(_tick(price=10000, seconds=1))
    sigs = s.on_tick(_tick(price=10260, seconds=10))  # +2.6% > 2.5%
    assert len(sigs) == 1
    assert sigs[0].side == Side.SELL
    assert "take-profit" in sigs[0].note


def test_stop_loss_exit():
    s = _strat()
    s.on_event(_reach())
    s.on_tick(_tick(price=10000, seconds=1))
    sigs = s.on_tick(_tick(price=9849, seconds=10))  # -1.51% > 1.5%
    assert len(sigs) == 1
    assert sigs[0].side == Side.SELL
    assert "stop-loss" in sigs[0].note
    assert sigs[0].urgency == "high"


def test_no_exit_within_band():
    s = _strat()
    s.on_event(_reach())
    s.on_tick(_tick(price=10000, seconds=1))
    sigs = s.on_tick(_tick(price=10100, seconds=5))  # +1%, neither tp nor sl
    assert sigs == []


# Hard timeout -------------------------------------------------------------


def test_hold_timeout_exit():
    s = _strat(hold_timeout_seconds=300)
    s.on_event(_reach())
    s.on_tick(_tick(price=10000, seconds=1))
    sigs = s.on_tick(_tick(price=10000, seconds=400))  # > 300s
    assert len(sigs) == 1
    assert "timeout" in sigs[0].note


# Flat timeout (5분 룰의 정확한 의미) -----------------------------------


def test_flat_timeout_exit_when_no_followthrough():
    """Price stays in ±0.5% band for ≥ flat_timeout → exit."""
    s = _strat(flat_timeout_seconds=60, flat_band_pct=0.5)
    s.on_event(_reach())
    s.on_tick(_tick(price=10000, seconds=1))  # entry
    # tick around within band but never breaks out
    assert s.on_tick(_tick(price=10020, seconds=20)) == []
    assert s.on_tick(_tick(price=9985, seconds=40)) == []
    sigs = s.on_tick(_tick(price=10010, seconds=70))  # > 60s in band
    assert len(sigs) == 1
    assert "flat" in sigs[0].note


def test_flat_timer_resets_when_price_moves_out_of_band():
    s = _strat(flat_timeout_seconds=60, flat_band_pct=0.5)
    s.on_event(_reach())
    s.on_tick(_tick(price=10000, seconds=1))
    # 50초 이내 횡보
    assert s.on_tick(_tick(price=10010, seconds=30)) == []
    # 가격이 +0.7% 로 band 이탈 → flat timer reset
    assert s.on_tick(_tick(price=10070, seconds=50)) == []
    # 다시 band 안으로 돌아왔지만 reset 후 70초가 안 지났으니 아직 안 나감
    assert s.on_tick(_tick(price=10005, seconds=80)) == []
    # 110초 (50+60) 까지 band 유지 → exit
    sigs = s.on_tick(_tick(price=10010, seconds=120))
    assert len(sigs) == 1
    assert "flat" in sigs[0].note


# Buy-criteria 훼손 (LimitUpBroken) -------------------------------------


def test_exit_on_leader_limit_up_broken():
    s = _strat()
    s.on_event(_reach())
    s.on_tick(_tick(price=10000, seconds=1))
    sigs = s.on_event(_broken())
    assert len(sigs) == 1
    assert sigs[0].symbol == "FOLLOW"
    assert sigs[0].side == Side.SELL
    assert sigs[0].urgency == "high"
    assert "broken" in sigs[0].note


def test_broken_for_unrelated_leader_no_exit():
    s = PairFollowStrategy(
        pairs={"LEADER1": "FOLLOW1", "LEADER2": "FOLLOW2"},
    )
    s.on_event(_reach(symbol="LEADER1"))
    s.on_tick(Tick(symbol="FOLLOW1", timestamp=_ts(1), price=10000, volume=10))
    # LEADER2 broken (no position open for it)
    sigs = s.on_event(_broken(symbol="LEADER2"))
    assert sigs == []


# Multiple pairs ----------------------------------------------------------


def test_multiple_pairs_independent():
    s = PairFollowStrategy(
        pairs={"LEADER1": "FOLLOW1", "LEADER2": "FOLLOW2"},
    )
    sigs1 = s.on_event(_reach(symbol="LEADER1"))
    sigs2 = s.on_event(_reach(symbol="LEADER2"))
    assert len(sigs1) == 1 and sigs1[0].symbol == "FOLLOW1"
    assert len(sigs2) == 1 and sigs2[0].symbol == "FOLLOW2"
    assert len(s.open_positions()) == 2


# Validation --------------------------------------------------------------


def test_rejects_empty_pairs():
    with pytest.raises(ValueError, match="pairs must not be empty"):
        PairFollowStrategy(pairs={})


def test_rejects_invalid_pcts():
    with pytest.raises(ValueError):
        PairFollowStrategy(pairs={"L": "F"}, take_profit_pct=-1)
    with pytest.raises(ValueError):
        PairFollowStrategy(pairs={"L": "F"}, stop_loss_pct=0)


def test_rejects_invalid_timeouts():
    with pytest.raises(ValueError, match=">= flat_timeout"):
        PairFollowStrategy(pairs={"L": "F"}, hold_timeout_seconds=30, flat_timeout_seconds=60)


def test_rejects_invalid_confidence():
    with pytest.raises(ValueError):
        PairFollowStrategy(pairs={"L": "F"}, confidence=1.5)
    with pytest.raises(ValueError):
        PairFollowStrategy(pairs={"L": "F"}, confidence=0)
