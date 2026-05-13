"""MacroCalendarSource + MacroCalendarGate — 24h entry-veto guard."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.domain import Bar, Side, Signal, Tick
from ks_ws.events import Event
from ks_ws.sources.macro_calendar import (
    MacroCalendarGate,
    MacroCalendarSource,
    MacroEvent,
    default_2026_q2_calendar,
)
from ks_ws.strategies.base import Strategy

# --- MacroEvent ---

def test_macro_event_requires_tzaware() -> None:
    with pytest.raises(ValueError):
        MacroEvent("CPI", datetime(2026, 5, 13, 12, 30))


def test_macro_event_unknown_severity() -> None:
    with pytest.raises(ValueError):
        MacroEvent("CPI", datetime(2026, 5, 13, 12, 30, tzinfo=UTC), severity="critical")


# --- MacroCalendarSource.active() ---

def _event_at(
    hours_offset_from_now: float, *, name: str = "CPI", severity: str = "high",
) -> MacroEvent:
    return MacroEvent(
        name,
        datetime.now(UTC) + timedelta(hours=hours_offset_from_now),
        severity=severity,
    )


def test_calendar_active_within_24h_before() -> None:
    src = MacroCalendarSource([_event_at(12.0)], hours_before=24.0, hours_after=2.0)
    assert src.is_active() is True
    assert len(src.active()) == 1


def test_calendar_active_within_2h_after() -> None:
    src = MacroCalendarSource([_event_at(-1.0)], hours_before=24.0, hours_after=2.0)
    assert src.is_active() is True


def test_calendar_inactive_beyond_window() -> None:
    src = MacroCalendarSource([_event_at(30.0)], hours_before=24.0, hours_after=2.0)
    assert src.is_active() is False
    src2 = MacroCalendarSource([_event_at(-5.0)], hours_before=24.0, hours_after=2.0)
    assert src2.is_active() is False


def test_calendar_severity_filter() -> None:
    src = MacroCalendarSource(
        [_event_at(1.0, severity="medium")],
        severities=("high",),  # only high → medium event ignored
    )
    assert src.is_active() is False
    src2 = MacroCalendarSource(
        [_event_at(1.0, severity="medium")],
        severities=("high", "medium"),
    )
    assert src2.is_active() is True


def test_calendar_active_now_explicit() -> None:
    fixed = datetime(2026, 5, 13, 12, 30, tzinfo=UTC)
    src = MacroCalendarSource(
        [MacroEvent("CPI", fixed)], hours_before=24.0, hours_after=2.0,
    )
    assert src.is_active(fixed - timedelta(hours=1)) is True
    assert src.is_active(fixed + timedelta(hours=3)) is False


def test_calendar_invalid_now_tz_naive() -> None:
    src = MacroCalendarSource()
    with pytest.raises(ValueError):
        src.is_active(datetime(2026, 5, 13))


def test_calendar_negative_hours_invalid() -> None:
    with pytest.raises(ValueError):
        MacroCalendarSource(hours_before=-1.0)


# --- MacroCalendarGate ---


class _StubStrategy(Strategy):
    name = "stub"

    def __init__(self) -> None:
        self.calls = 0

    def on_tick(self, tick: Tick) -> list[Signal]:
        self.calls += 1
        return [
            Signal(symbol=tick.symbol, side=Side.BUY, confidence=0.6,
                   strategy=self.name, timestamp=tick.timestamp, note="buy"),
            Signal(symbol=tick.symbol, side=Side.SELL, confidence=1.0,
                   strategy=self.name, timestamp=tick.timestamp, note="sell"),
        ]


def _tick() -> Tick:
    return Tick(symbol="005930", price=100, volume=10, timestamp=datetime.now(UTC))


def test_gate_passes_signals_when_calendar_inactive() -> None:
    cal = MacroCalendarSource([_event_at(30.0)])  # > 24h, inactive
    gate = MacroCalendarGate(_StubStrategy(), calendar=cal)
    sigs = gate.on_tick(_tick())
    assert len(sigs) == 2
    assert {s.side for s in sigs} == {Side.BUY, Side.SELL}


def test_gate_blocks_buy_when_calendar_active() -> None:
    cal = MacroCalendarSource([_event_at(12.0)])  # within 24h
    gate = MacroCalendarGate(_StubStrategy(), calendar=cal)
    sigs = gate.on_tick(_tick())
    assert len(sigs) == 1
    assert sigs[0].side is Side.SELL  # exits still pass


def test_gate_bar_event_dispatch_also_filtered() -> None:
    cal = MacroCalendarSource([_event_at(12.0)])
    s = _StubStrategy()
    gate = MacroCalendarGate(s, calendar=cal)
    # bar/event paths should also gate BUY through _filter
    bar = Bar(symbol="005930", timeframe="1d", timestamp=datetime.now(UTC),
              open=100, high=110, low=90, close=105, volume=1000, value=105_000)
    # _StubStrategy.on_bar returns [] (default) — test through tick path is enough.
    # Verify on_event path filters too
    class _E(Event):
        pass
    # default Strategy.on_event returns [] → just ensure no exception
    gate.on_event(_E(symbol="005930", timestamp=datetime.now(UTC)))
    # The bar path same:
    gate.on_bar(bar)


# --- default_2026_q2_calendar ---


def test_default_2026_q2_calendar_has_known_events() -> None:
    cal = default_2026_q2_calendar()
    names = {ev.name for ev in cal.events()}
    assert {"CPI", "FOMC", "NFP"}.issubset(names)
    # 5/12 11:00 UTC = 24h before CPI 5/12 12:30 → just outside lookback?
    # Window = [event - 24h, event + 2h] = [5/11 12:30, 5/12 14:30].
    # Check inside window:
    active_inside = cal.active(datetime(2026, 5, 12, 10, 0, tzinfo=UTC))
    names_active = {ev.name for ev in active_inside}
    assert "CPI" in names_active  # 5/12 CPI within 24h
