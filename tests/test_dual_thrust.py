"""DualThrustStrategy — Michael Chalek 양방향 변동성 돌파."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ks_ws.domain import Bar, Side, Tick
from ks_ws.storage.bars import BarStore
from ks_ws.strategies.dual_thrust import (
    DualThrustStrategy,
    compute_dual_thrust_range,
    compute_dual_thrust_ranges,
)


def _tick(price: int, *, ts_offset_min: int = 0,
          sym: str = "005930") -> Tick:
    base = datetime(2026, 5, 13, tzinfo=UTC)
    return Tick(
        symbol=sym, price=price, volume=100,
        timestamp=base + timedelta(minutes=ts_offset_min),
    )


def _bar(*, h: int, low: int, c: int, days_ago: int = 0) -> Bar:
    base = datetime(2026, 5, 13, tzinfo=UTC)
    return Bar(
        symbol="005930", timeframe="1d",
        timestamp=base - timedelta(days=days_ago),
        open=(h + low) // 2, high=h, low=low, close=c,
        volume=1_000, value=c * 1_000,
    )


def test_compute_range_formula() -> None:
    # HH=120, LC=95, HC=115, LL=80 → range = max(120-95, 115-80) = max(25, 35) = 35
    bars = [
        _bar(h=110, low=90, c=100, days_ago=4),
        _bar(h=115, low=85, c=105, days_ago=3),
        _bar(h=120, low=95, c=115, days_ago=2),
        _bar(h=118, low=80, c=98, days_ago=1),
        _bar(h=112, low=92, c=102, days_ago=0),
    ]
    r = compute_dual_thrust_range(bars)
    assert r == 35


def test_compute_range_empty() -> None:
    assert compute_dual_thrust_range([]) == 0
    assert compute_dual_thrust_range([_bar(h=100, low=90, c=95)]) == 0


def test_entry_on_buy_trigger_cross() -> None:
    # range = 20, k1 = 0.5 → buy offset = 10
    # day open = 100 → buy_trigger = 110
    s = DualThrustStrategy(ranges={"005930": 20}, k1=0.5, k2=0.5)
    assert s.on_tick(_tick(100)) == []  # day open
    assert s.on_tick(_tick(108, ts_offset_min=1)) == []  # below trigger
    sigs = s.on_tick(_tick(112, ts_offset_min=2))  # cross
    assert len(sigs) == 1
    assert sigs[0].side is Side.BUY
    assert sigs[0].strategy == "dual_thrust"


def test_no_entry_below_buy_trigger() -> None:
    s = DualThrustStrategy(ranges={"005930": 20}, k1=0.5, k2=0.5)
    s.on_tick(_tick(100))
    assert s.on_tick(_tick(109, ts_offset_min=1)) == []


def test_sell_trigger_exits_position() -> None:
    s = DualThrustStrategy(ranges={"005930": 20}, k1=0.5, k2=0.5)
    s.on_tick(_tick(100))
    s.on_tick(_tick(112, ts_offset_min=1))  # entry @ 112
    # sell_trigger = 100 - 0.5*20 = 90
    sigs = s.on_tick(_tick(89, ts_offset_min=2))
    assert sigs and sigs[0].side is Side.SELL
    assert sigs[0].urgency == "high"


def test_tp_exit() -> None:
    s = DualThrustStrategy(
        ranges={"005930": 20}, k1=0.5, k2=0.5,
        take_profit_pct=3.0, stop_loss_pct=2.0,
    )
    s.on_tick(_tick(100))
    s.on_tick(_tick(112, ts_offset_min=1))  # entry @ 112
    sigs = s.on_tick(_tick(116, ts_offset_min=2))  # 112 * 1.03 = 115.36 → TP
    assert sigs and sigs[0].side is Side.SELL


def test_no_range_no_entry() -> None:
    s = DualThrustStrategy(ranges={}, k1=0.5, k2=0.5)
    s.on_tick(_tick(100))
    assert s.on_tick(_tick(200, ts_offset_min=1)) == []


def test_invalid_k_raises() -> None:
    with pytest.raises(ValueError):
        DualThrustStrategy(ranges={}, k1=0, k2=0.5)
    with pytest.raises(ValueError):
        DualThrustStrategy(ranges={}, k1=0.5, k2=3)


def test_compute_ranges_from_bar_store(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    bars = [
        Bar(symbol="005930", timeframe="1d",
            timestamp=datetime(2026, 5, 13, tzinfo=UTC) - timedelta(days=5 - i),
            open=100, high=110 + i, low=90 - i, close=100,
            volume=1_000, value=100_000)
        for i in range(5)
    ]
    store.write(bars)
    ranges = compute_dual_thrust_ranges(store, ["005930"], lookback=5)
    assert "005930" in ranges
    assert ranges["005930"] > 0


def test_no_double_entry_same_day() -> None:
    s = DualThrustStrategy(ranges={"005930": 20}, k1=0.5, k2=0.5)
    s.on_tick(_tick(100))
    s.on_tick(_tick(112, ts_offset_min=1))  # entry
    s.on_tick(_tick(120, ts_offset_min=2))  # TP exit
    # Same day re-cross
    assert s.on_tick(_tick(115, ts_offset_min=3)) == []
