"""Tests for InstFgnFlowStrategy."""

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from ks_ws.domain import Side
from ks_ws.events import ForeignNetBuy, ProgramFlowEnter, ProgramFlowExit
from ks_ws.strategies.inst_fgn_flow import InstFgnFlowStrategy

_KST = ZoneInfo("Asia/Seoul")


def _ts(hour=14, minute=0):
    return datetime(2026, 5, 11, hour, minute, tzinfo=_KST).astimezone(UTC)


def _strat(**overrides):
    defaults = dict(
        entry_after_kst=time(13, 30),
        min_fgn_streak=2,
        confidence=0.6,
    )
    defaults.update(overrides)
    return InstFgnFlowStrategy(**defaults)


def _fgn(symbol="X", delta=1_000_000_000, ts=None):
    return ForeignNetBuy(
        symbol=symbol, timestamp=ts or _ts(), delta_krw=delta, window_seconds=300
    )


def _prog_enter(symbol="X", delta=2_000_000_000, ts=None):
    return ProgramFlowEnter(
        symbol=symbol, timestamp=ts or _ts(), delta_krw=delta, window_seconds=300
    )


def _prog_exit(symbol="X", ts=None):
    return ProgramFlowExit(
        symbol=symbol, timestamp=ts or _ts(), delta_krw=-500_000_000, window_seconds=300
    )


# Validation ---------------------------------------------------------------


def test_validation():
    with pytest.raises(ValueError):
        InstFgnFlowStrategy(min_fgn_streak=0)
    with pytest.raises(ValueError):
        InstFgnFlowStrategy(confidence=2)


# Entry --------------------------------------------------------------------


def test_no_entry_without_fgn_streak():
    strat = _strat(min_fgn_streak=3)
    strat.on_event(_prog_enter())
    sigs = strat.on_event(_fgn(delta=1_000_000_000))  # streak 1
    assert sigs == []


def test_no_entry_without_program_enter():
    strat = _strat(min_fgn_streak=2)
    strat.on_event(_fgn())
    sigs = strat.on_event(_fgn())  # streak 2 but no program enter
    assert sigs == []


def test_entry_after_streak_and_program():
    strat = _strat(min_fgn_streak=2)
    strat.on_event(_prog_enter())
    strat.on_event(_fgn())  # streak 1, but already prog → check entry
    sigs = strat.on_event(_fgn())  # streak 2 → BUY
    assert len(sigs) == 1
    assert sigs[0].side == Side.BUY


def test_no_entry_before_1330():
    strat = _strat(min_fgn_streak=2)
    early = _ts(13, 0)
    strat.on_event(_prog_enter(ts=early))
    strat.on_event(_fgn(ts=early))
    sigs = strat.on_event(_fgn(ts=early))
    assert sigs == []


def test_watchlist_filter():
    strat = _strat(watchlist={"OTHER"}, min_fgn_streak=2)
    strat.on_event(_prog_enter(symbol="X"))
    sigs = strat.on_event(_fgn(symbol="X"))
    assert sigs == []


def test_no_double_entry_when_already_in_position():
    strat = _strat(min_fgn_streak=2)
    strat.on_event(_prog_enter())
    strat.on_event(_fgn())
    strat.on_event(_fgn())  # entry
    sigs = strat.on_event(_fgn())  # already in
    assert sigs == []


# Exit --------------------------------------------------------------------


def test_exit_on_negative_foreign_flow():
    strat = _strat(min_fgn_streak=2)
    strat.on_event(_prog_enter())
    strat.on_event(_fgn())
    strat.on_event(_fgn())  # entry
    sigs = strat.on_event(_fgn(delta=-2_000_000_000))
    assert len(sigs) == 1
    assert sigs[0].side == Side.SELL
    assert "foreign net SELL" in sigs[0].note


def test_exit_on_program_exit():
    strat = _strat(min_fgn_streak=2)
    strat.on_event(_prog_enter())
    strat.on_event(_fgn())
    strat.on_event(_fgn())
    sigs = strat.on_event(_prog_exit())
    assert len(sigs) == 1
    assert sigs[0].side == Side.SELL


def test_negative_fgn_resets_streak_when_not_in_position():
    strat = _strat(min_fgn_streak=3)
    strat.on_event(_prog_enter())
    strat.on_event(_fgn())  # streak 1
    strat.on_event(_fgn(delta=-500_000_000))  # reset
    strat.on_event(_fgn())  # streak 1 again
    sigs = strat.on_event(_fgn())  # streak 2 — still below 3
    assert sigs == []
