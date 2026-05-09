"""Tests for DojiCandleDetector + ClosingBetStrategy."""

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from ks_ws.detectors.doji import DojiCandleDetector
from ks_ws.domain import Bar, Side, Tick
from ks_ws.events import DojiCandle
from ks_ws.strategies.closing_bet import ClosingBetStrategy

_KST = ZoneInfo("Asia/Seoul")


def _kst(year, month, day, hour=15, minute=20):
    return datetime(year, month, day, hour, minute, tzinfo=_KST).astimezone(UTC)


def _bar(open=10000, high=10100, low=9900, close=10010, ts=None):
    return Bar(
        symbol="A005930",
        timestamp=ts or _kst(2026, 5, 11, 15, 20),
        timeframe="1d",
        open=open,
        high=high,
        low=low,
        close=close,
        volume=1000,
        value=open * 1000,
    )


# DojiCandleDetector -------------------------------------------------------


def test_detector_emits_on_doji():
    events = []
    det = DojiCandleDetector(emit=events.append)
    # body = |10000-10010|/10000 = 0.1% < 0.3, range = 200/10000 = 2% > 0.5
    det.feed_bar(_bar(open=10000, high=10100, low=9900, close=10010))
    assert len(events) == 1
    assert isinstance(events[0], DojiCandle)
    assert events[0].body_pct < 0.3


def test_detector_skips_large_body():
    events = []
    det = DojiCandleDetector(emit=events.append)
    # body = 5% — not a doji
    det.feed_bar(_bar(open=10000, high=10500, low=9900, close=10500))
    assert events == []


def test_detector_skips_low_range():
    events = []
    det = DojiCandleDetector(emit=events.append, range_pct_min=1.0)
    # body = 0.05% (doji) but range = 0.2% (too small)
    det.feed_bar(_bar(open=10000, high=10010, low=9990, close=10005))
    assert events == []


def test_detector_only_specified_timeframe():
    events = []
    det = DojiCandleDetector(emit=events.append, timeframe="1d")
    bar_5m = Bar(
        symbol="X",
        timestamp=_kst(2026, 5, 11),
        timeframe="5m",
        open=10000, high=10100, low=9900, close=10010,
        volume=100, value=1_000_000,
    )
    det.feed_bar(bar_5m)
    assert events == []


def test_detector_validation():
    with pytest.raises(ValueError):
        DojiCandleDetector(emit=lambda e: None, body_pct_threshold=0)
    with pytest.raises(ValueError):
        DojiCandleDetector(emit=lambda e: None, range_pct_min=-1)


# ClosingBetStrategy -------------------------------------------------------


def _doji_event(symbol="A005930", ts=None, body=0.1, range_pct=2.0):
    return DojiCandle(
        symbol=symbol,
        timestamp=ts or _kst(2026, 5, 11, 15, 20),
        body_pct=body,
        range_pct=range_pct,
        direction_hint="neutral",
    )


def test_entry_on_doji_after_1330():
    strat = ClosingBetStrategy()
    sigs = strat.on_event(_doji_event(ts=_kst(2026, 5, 11, 13, 30)))
    assert len(sigs) == 1
    assert sigs[0].side == Side.BUY


def test_no_entry_before_1330():
    strat = ClosingBetStrategy(entry_after_kst=time(13, 30))
    sigs = strat.on_event(_doji_event(ts=_kst(2026, 5, 11, 13, 29)))
    assert sigs == []


def test_no_double_entry_when_already_open():
    strat = ClosingBetStrategy()
    strat.on_event(_doji_event(ts=_kst(2026, 5, 11, 15, 20)))
    sigs = strat.on_event(_doji_event(ts=_kst(2026, 5, 11, 15, 25)))
    assert sigs == []


def test_watchlist_filter():
    strat = ClosingBetStrategy(watchlist={"OTHER"})
    sigs = strat.on_event(_doji_event(symbol="A005930"))
    assert sigs == []


def test_next_day_first_tick_captures_entry_no_exit():
    strat = ClosingBetStrategy(take_profit_pct=2.0, stop_loss_pct=3.0)
    strat.on_event(_doji_event(ts=_kst(2026, 5, 11, 15, 20)))
    # Next day first tick at 09:00 — should set entry_price (no signal)
    sigs = strat.on_tick(
        Tick(
            symbol="A005930",
            timestamp=_kst(2026, 5, 12, 9, 0),
            price=10000,
            volume=10,
        )
    )
    assert sigs == []
    assert strat.open_positions()["A005930"].entry_price == 10000


def test_next_day_take_profit():
    strat = ClosingBetStrategy(take_profit_pct=2.0, stop_loss_pct=3.0)
    strat.on_event(_doji_event(ts=_kst(2026, 5, 11, 15, 20)))
    strat.on_tick(Tick(symbol="A005930", timestamp=_kst(2026, 5, 12, 9, 0), price=10000, volume=10))
    sigs = strat.on_tick(
        Tick(symbol="A005930", timestamp=_kst(2026, 5, 12, 9, 5), price=10200, volume=10)
    )
    assert len(sigs) == 1
    assert sigs[0].side == Side.SELL
    assert "take-profit" in sigs[0].note


def test_next_day_stop_loss():
    strat = ClosingBetStrategy(take_profit_pct=2.0, stop_loss_pct=3.0)
    strat.on_event(_doji_event(ts=_kst(2026, 5, 11, 15, 20)))
    strat.on_tick(Tick(symbol="A005930", timestamp=_kst(2026, 5, 12, 9, 0), price=10000, volume=10))
    sigs = strat.on_tick(
        Tick(symbol="A005930", timestamp=_kst(2026, 5, 12, 9, 5), price=9700, volume=10)
    )
    assert len(sigs) == 1
    assert sigs[0].side == Side.SELL
    assert sigs[0].urgency == "high"


def test_no_exit_within_band():
    strat = ClosingBetStrategy()
    strat.on_event(_doji_event(ts=_kst(2026, 5, 11, 15, 20)))
    strat.on_tick(Tick(symbol="A005930", timestamp=_kst(2026, 5, 12, 9, 0), price=10000, volume=10))
    sigs = strat.on_tick(
        Tick(symbol="A005930", timestamp=_kst(2026, 5, 12, 9, 5), price=10100, volume=10)  # +1%
    )
    assert sigs == []


def test_validation_rejects_invalid_pcts():
    with pytest.raises(ValueError):
        ClosingBetStrategy(take_profit_pct=0)
    with pytest.raises(ValueError):
        ClosingBetStrategy(stop_loss_pct=-1)
    with pytest.raises(ValueError):
        ClosingBetStrategy(confidence=2)
