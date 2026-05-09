"""Tests for TimeWindowGate / RegimeGate."""

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from ks_ws.domain import Bar, OrderBook, OrderBookLevel, Side, Signal, Tick
from ks_ws.events import GapUp
from ks_ws.strategies.base import Strategy
from ks_ws.strategies.gates import RegimeGate, TimeWindowGate

_KST = ZoneInfo("Asia/Seoul")


class _AlwaysBuy(Strategy):
    name = "always_buy"

    def on_bar(self, bar):
        return [self._sig(bar.symbol, bar.timestamp)]

    def on_tick(self, tick):
        return [self._sig(tick.symbol, tick.timestamp)]

    def on_orderbook(self, orderbook):
        return [self._sig(orderbook.symbol, orderbook.timestamp)]

    def on_event(self, event):
        return [self._sig(event.symbol, event.timestamp)]

    def _sig(self, symbol, ts):
        return Signal(
            symbol=symbol,
            side=Side.BUY,
            confidence=0.5,
            strategy=self.name,
            timestamp=ts,
        )


def _bar(ts):
    return Bar(
        symbol="A005930",
        timestamp=ts,
        timeframe="1m",
        open=70000,
        high=70100,
        low=69900,
        close=70050,
        volume=1000,
        value=70_050_000,
    )


def _tick(ts):
    return Tick(symbol="A005930", timestamp=ts, price=70000, volume=10)


def _orderbook(ts):
    return OrderBook(
        symbol="A005930",
        timestamp=ts,
        bids=(OrderBookLevel(price=70000, volume=100),),
        asks=(OrderBookLevel(price=70050, volume=100),),
    )


def _kst(hour, minute):
    """Build a UTC timestamp that corresponds to today HH:MM in KST."""
    return datetime(2026, 5, 11, hour, minute, tzinfo=_KST).astimezone(UTC)


# TimeWindowGate -----------------------------------------------------------


def test_time_window_gate_inside_window_passes_bar():
    gate = TimeWindowGate(_AlwaysBuy(), windows=[(time(9, 0), time(9, 50))])
    sigs = gate.on_bar(_bar(_kst(9, 30)))
    assert len(sigs) == 1


def test_time_window_gate_outside_window_returns_empty():
    gate = TimeWindowGate(_AlwaysBuy(), windows=[(time(9, 0), time(9, 50))])
    assert gate.on_bar(_bar(_kst(10, 0))) == []
    assert gate.on_bar(_bar(_kst(8, 59))) == []


def test_time_window_gate_multiple_windows():
    gate = TimeWindowGate(
        _AlwaysBuy(),
        windows=[(time(9, 0), time(9, 50)), (time(13, 30), time(15, 30))],
    )
    assert len(gate.on_bar(_bar(_kst(9, 5)))) == 1
    assert len(gate.on_bar(_bar(_kst(14, 0)))) == 1
    assert gate.on_bar(_bar(_kst(11, 0))) == []


def test_time_window_gate_dispatches_all_hooks():
    gate = TimeWindowGate(_AlwaysBuy(), windows=[(time(9, 0), time(9, 50))])
    ts = _kst(9, 30)
    assert gate.on_tick(_tick(ts)) != []
    assert gate.on_orderbook(_orderbook(ts)) != []
    assert gate.on_event(GapUp(symbol="A005930", timestamp=ts, gap_pct=5.0)) != []


def test_time_window_gate_preserves_inner_name():
    gate = TimeWindowGate(_AlwaysBuy(), windows=[(time(9, 0), time(9, 50))])
    assert gate.name == "always_buy"


def test_time_window_gate_rejects_naive_timestamp():
    gate = TimeWindowGate(_AlwaysBuy(), windows=[(time(9, 0), time(9, 50))])
    naive = datetime(2026, 5, 11, 9, 30)
    with pytest.raises(ValueError, match="timezone-aware"):
        gate.is_active(naive)


def test_time_window_gate_rejects_invalid_window():
    with pytest.raises(ValueError, match="must be < end"):
        TimeWindowGate(_AlwaysBuy(), windows=[(time(10, 0), time(9, 0))])
    with pytest.raises(ValueError, match="must not be empty"):
        TimeWindowGate(_AlwaysBuy(), windows=[])


def test_time_window_gate_half_open_boundary():
    """[start, end) — start inclusive, end exclusive."""
    gate = TimeWindowGate(_AlwaysBuy(), windows=[(time(9, 0), time(9, 50))])
    assert len(gate.on_bar(_bar(_kst(9, 0)))) == 1  # start inclusive
    assert gate.on_bar(_bar(_kst(9, 50))) == []  # end exclusive


# RegimeGate ---------------------------------------------------------------


def test_regime_gate_passes_when_regime_allowed():
    regime = "sideways"
    gate = RegimeGate(_AlwaysBuy(), allowed={"sideways", "downtrend"}, regime_provider=lambda: regime)
    assert len(gate.on_bar(_bar(_kst(9, 30)))) == 1


def test_regime_gate_blocks_when_regime_not_allowed():
    gate = RegimeGate(_AlwaysBuy(), allowed={"sideways"}, regime_provider=lambda: "uptrend")
    assert gate.on_bar(_bar(_kst(9, 30))) == []


def test_regime_gate_treats_unknown_as_blocked():
    gate = RegimeGate(_AlwaysBuy(), allowed={"sideways"}, regime_provider=lambda: "unknown")
    assert gate.on_bar(_bar(_kst(9, 30))) == []


def test_regime_gate_dynamic_regime_change():
    state = {"regime": "uptrend"}
    gate = RegimeGate(
        _AlwaysBuy(), allowed={"sideways"}, regime_provider=lambda: state["regime"]
    )
    assert gate.on_bar(_bar(_kst(9, 30))) == []
    state["regime"] = "sideways"
    assert len(gate.on_bar(_bar(_kst(9, 30)))) == 1


def test_regime_gate_rejects_empty_allowed():
    with pytest.raises(ValueError, match="must not be empty"):
        RegimeGate(_AlwaysBuy(), allowed=set(), regime_provider=lambda: "sideways")


def test_regime_gate_preserves_inner_name():
    gate = RegimeGate(_AlwaysBuy(), allowed={"sideways"}, regime_provider=lambda: "sideways")
    assert gate.name == "always_buy"


# Composition --------------------------------------------------------------


def test_time_then_regime_compose():
    """TimeWindowGate(RegimeGate(...)) — both must allow."""
    state = {"regime": "sideways"}
    inner = RegimeGate(
        _AlwaysBuy(), allowed={"sideways"}, regime_provider=lambda: state["regime"]
    )
    outer = TimeWindowGate(inner, windows=[(time(9, 0), time(9, 50))])
    # both pass
    assert len(outer.on_bar(_bar(_kst(9, 30)))) == 1
    # time fails
    assert outer.on_bar(_bar(_kst(10, 0))) == []
    # regime fails
    state["regime"] = "uptrend"
    assert outer.on_bar(_bar(_kst(9, 30))) == []
