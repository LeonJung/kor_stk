"""ForeignFlowStrategy — 외인 순매수 spike → BUY."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.domain import Side, Tick
from ks_ws.events import ForeignNetBuy
from ks_ws.strategies.foreign_flow_strategy import ForeignFlowStrategy


def _tick(price: int, *, ts_offset_min: int = 0,
          sym: str = "005930") -> Tick:
    base = datetime(2026, 5, 13, tzinfo=UTC)
    return Tick(
        symbol=sym, price=price, volume=100,
        timestamp=base + timedelta(minutes=ts_offset_min),
    )


def _flow_event(delta: int, *, sym: str = "005930") -> ForeignNetBuy:
    return ForeignNetBuy(
        symbol=sym, timestamp=datetime(2026, 5, 13, tzinfo=UTC),
        delta_krw=delta, window_seconds=60,
    )


def test_entry_on_tick_after_strong_flow() -> None:
    s = ForeignFlowStrategy(strong_threshold_krw=100_000_000_000)
    s.on_event(_flow_event(150_000_000_000))  # +1500억
    sigs = s.on_tick(_tick(100))
    assert len(sigs) == 1
    assert sigs[0].side is Side.BUY
    assert sigs[0].strategy == "foreign_flow"


def test_no_entry_without_strong_flow() -> None:
    s = ForeignFlowStrategy(strong_threshold_krw=100_000_000_000)
    s.on_event(_flow_event(50_000_000_000))  # weak
    assert s.on_tick(_tick(100)) == []


def test_no_entry_on_negative_flow() -> None:
    s = ForeignFlowStrategy()
    s.on_event(_flow_event(-200_000_000_000))  # 매도
    assert s.on_tick(_tick(100)) == []


def test_tp_exit() -> None:
    s = ForeignFlowStrategy(
        strong_threshold_krw=100_000_000_000,
        take_profit_pct=3.0, stop_loss_pct=2.0,
    )
    s.on_event(_flow_event(150_000_000_000))
    s.on_tick(_tick(100))  # entry @ 100
    sigs = s.on_tick(_tick(104, ts_offset_min=1))  # +4% > TP 3%
    assert sigs and sigs[0].side is Side.SELL


def test_sl_exit() -> None:
    s = ForeignFlowStrategy(
        strong_threshold_krw=100_000_000_000,
        take_profit_pct=3.0, stop_loss_pct=2.0,
    )
    s.on_event(_flow_event(150_000_000_000))
    s.on_tick(_tick(100))
    sigs = s.on_tick(_tick(97, ts_offset_min=1))  # -3% < SL 2%
    assert sigs and sigs[0].side is Side.SELL
    assert sigs[0].urgency == "high"


def test_watchlist_filter() -> None:
    s = ForeignFlowStrategy(watchlist={"000660"})
    s.on_event(_flow_event(150_000_000_000, sym="005930"))
    assert s.on_tick(_tick(100, sym="005930")) == []


def test_no_double_entry_same_day() -> None:
    s = ForeignFlowStrategy()
    s.on_event(_flow_event(150_000_000_000))
    s.on_tick(_tick(100))  # entry
    s.on_tick(_tick(104, ts_offset_min=1))  # TP exit
    # New flow event same day
    s.on_event(_flow_event(200_000_000_000))
    assert s.on_tick(_tick(110, ts_offset_min=2)) == []


def test_ignores_other_events() -> None:
    from ks_ws.events import Event

    class _Other(Event):
        pass
    s = ForeignFlowStrategy()
    assert s.on_event(_Other(symbol="005930",
                             timestamp=datetime.now(UTC))) == []


def test_invalid_threshold_raises() -> None:
    with pytest.raises(ValueError):
        ForeignFlowStrategy(strong_threshold_krw=0)


def test_invalid_pct_raises() -> None:
    with pytest.raises(ValueError):
        ForeignFlowStrategy(take_profit_pct=0)
