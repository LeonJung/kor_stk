"""NR7BreakoutStrategy — 7일 최소 range 후 돌파."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ks_ws.domain import Bar, Side, Tick
from ks_ws.storage.bars import BarStore
from ks_ws.strategies.nr7_breakout import (
    NR7BreakoutStrategy,
    compute_nr7_setup,
    is_nr7,
)


def _tick(price: int, *, ts_offset_min: int = 0,
          sym: str = "005930") -> Tick:
    base = datetime(2026, 5, 13, 0, 0, tzinfo=UTC)  # UTC 00 = KST 09
    return Tick(
        symbol=sym, price=price, volume=100,
        timestamp=base + timedelta(minutes=ts_offset_min),
    )


def _bar(*, high: int, low: int, days_ago: int = 0) -> Bar:
    base = datetime(2026, 5, 13, tzinfo=UTC)
    return Bar(
        symbol="005930", timeframe="1d",
        timestamp=base - timedelta(days=days_ago),
        open=(high + low) // 2, high=high, low=low,
        close=(high + low) // 2,
        volume=1_000, value=(high + low) // 2 * 1_000,
    )


# --- is_nr7 ---


def test_is_nr7_true_when_last_is_min_range() -> None:
    bars = [
        _bar(high=110, low=90, days_ago=6),
        _bar(high=115, low=92, days_ago=5),
        _bar(high=120, low=95, days_ago=4),
        _bar(high=125, low=98, days_ago=3),
        _bar(high=130, low=100, days_ago=2),
        _bar(high=128, low=102, days_ago=1),
        _bar(high=110, low=105, days_ago=0),  # range 5 = min
    ]
    assert is_nr7(bars) is True


def test_is_nr7_false_when_other_smaller() -> None:
    bars = [
        _bar(high=110, low=105, days_ago=6),  # range 5 (smaller)
        _bar(high=115, low=92, days_ago=5),
        _bar(high=120, low=95, days_ago=4),
        _bar(high=125, low=98, days_ago=3),
        _bar(high=130, low=100, days_ago=2),
        _bar(high=128, low=102, days_ago=1),
        _bar(high=120, low=100, days_ago=0),  # range 20
    ]
    assert is_nr7(bars) is False


def test_is_nr7_false_with_fewer_than_7_bars() -> None:
    bars = [_bar(high=110, low=105, days_ago=i) for i in range(5)]
    assert is_nr7(bars) is False


# --- compute_nr7_setup ---


def test_compute_setup_from_bar_store(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    bars = [
        Bar(symbol="005930", timeframe="1d",
            timestamp=datetime(2026, 5, 13, tzinfo=UTC) - timedelta(days=6 - i),
            open=100, high=110 if i < 6 else 105, low=90 if i < 6 else 100,
            close=100, volume=1_000, value=100_000)
        for i in range(7)
    ]
    store.write(bars)
    setup = compute_nr7_setup(store, ["005930"])
    assert setup["005930"][0] == 105  # last bar high
    assert setup["005930"][1] is True  # range 5 < range 20


# --- NR7BreakoutStrategy ---


def test_entry_on_cross_above_prev_high() -> None:
    s = NR7BreakoutStrategy(setup={"005930": (110, True)})
    assert s.on_tick(_tick(105)) == []  # below
    sigs = s.on_tick(_tick(115, ts_offset_min=1))
    assert len(sigs) == 1
    assert sigs[0].side is Side.BUY
    assert sigs[0].strategy == "nr7_breakout"


def test_no_entry_when_not_nr7() -> None:
    s = NR7BreakoutStrategy(setup={"005930": (110, False)})
    assert s.on_tick(_tick(115)) == []


def test_no_entry_no_setup() -> None:
    s = NR7BreakoutStrategy(setup={})
    assert s.on_tick(_tick(115)) == []


def test_tp_exit() -> None:
    s = NR7BreakoutStrategy(
        setup={"005930": (110, True)},
        take_profit_pct=3.0, stop_loss_pct=2.0,
    )
    s.on_tick(_tick(105))
    s.on_tick(_tick(115, ts_offset_min=1))  # entry @ 115
    sigs = s.on_tick(_tick(119, ts_offset_min=2))  # 115 * 1.03 = 118.45 → TP
    assert sigs and sigs[0].side is Side.SELL


def test_sl_exit() -> None:
    s = NR7BreakoutStrategy(
        setup={"005930": (110, True)},
        take_profit_pct=3.0, stop_loss_pct=2.0,
    )
    s.on_tick(_tick(105))
    s.on_tick(_tick(115, ts_offset_min=1))
    sigs = s.on_tick(_tick(112, ts_offset_min=2))  # 115 * 0.98 = 112.7 → SL
    assert sigs and sigs[0].side is Side.SELL
    assert sigs[0].urgency == "high"


def test_no_double_entry_same_day() -> None:
    s = NR7BreakoutStrategy(setup={"005930": (110, True)})
    s.on_tick(_tick(105))
    s.on_tick(_tick(115, ts_offset_min=1))  # entry
    s.on_tick(_tick(119, ts_offset_min=2))  # TP exit (3% > prev)
    # Same-day re-cross should NOT re-enter
    assert s.on_tick(_tick(120, ts_offset_min=3)) == []


def test_edge_detection_no_repeat_buy() -> None:
    s = NR7BreakoutStrategy(setup={"005930": (110, True)})
    s.on_tick(_tick(105))
    sigs1 = s.on_tick(_tick(115, ts_offset_min=1))
    assert len(sigs1) == 1
    sigs2 = s.on_tick(_tick(116, ts_offset_min=2))  # Still above, but already in
    # Should not produce new BUY (already in position, exit logic only)
    assert all(s.side is not Side.BUY for s in sigs2)


def test_invalid_pct_raises() -> None:
    with pytest.raises(ValueError):
        NR7BreakoutStrategy(setup={}, take_profit_pct=0)
    with pytest.raises(ValueError):
        NR7BreakoutStrategy(setup={}, stop_loss_pct=-1)


def test_invalid_confidence_raises() -> None:
    with pytest.raises(ValueError):
        NR7BreakoutStrategy(setup={}, confidence=0)
    with pytest.raises(ValueError):
        NR7BreakoutStrategy(setup={}, confidence=1.5)
