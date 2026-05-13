"""ColorStreakStrategy — N 양봉 연속 후 prev_close 돌파 BUY."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ks_ws.domain import Bar, Side, Tick
from ks_ws.storage.bars import BarStore
from ks_ws.strategies.color_streak import (
    ColorStreakStrategy,
    compute_color_streak_setup,
    count_color_streak,
)


def _bar(*, o: int, c: int, days_ago: int = 0) -> Bar:
    base = datetime(2026, 5, 13, tzinfo=UTC)
    return Bar(
        symbol="005930", timeframe="1d",
        timestamp=base - timedelta(days=days_ago),
        open=o, high=max(o, c) + 5, low=min(o, c) - 5, close=c,
        volume=1_000, value=c * 1_000,
    )


def _tick(price: int, *, ts_offset_min: int = 0) -> Tick:
    base = datetime(2026, 5, 13, tzinfo=UTC)
    return Tick(symbol="005930", price=price, volume=100,
                timestamp=base + timedelta(minutes=ts_offset_min))


def test_count_streak_basic() -> None:
    bars = [
        _bar(o=100, c=95, days_ago=4),  # red
        _bar(o=95, c=100, days_ago=3),   # green
        _bar(o=100, c=105, days_ago=2),  # green
        _bar(o=105, c=110, days_ago=1),  # green
        _bar(o=110, c=115, days_ago=0),  # green
    ]
    assert count_color_streak(bars) == 4


def test_count_streak_zero_when_last_red() -> None:
    bars = [_bar(o=100, c=110), _bar(o=110, c=105)]
    assert count_color_streak(bars) == 0


def test_compute_setup_threshold(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    bars = [
        Bar(symbol="005930", timeframe="1d",
            timestamp=datetime(2026, 5, 13, tzinfo=UTC) - timedelta(days=4 - i),
            open=100 + i, high=100 + i + 5, low=95 + i,
            close=100 + i + 2,  # always green
            volume=1_000, value=100_000)
        for i in range(5)
    ]
    store.write(bars)
    setup = compute_color_streak_setup(store, ["005930"], min_streak=3)
    assert "005930" in setup
    prev_close, n = setup["005930"]
    assert n >= 3
    assert prev_close > 0


def test_entry_on_prev_close_cross() -> None:
    s = ColorStreakStrategy(setup={"005930": (100, 3)})
    assert s.on_tick(_tick(99)) == []
    sigs = s.on_tick(_tick(102, ts_offset_min=1))
    assert len(sigs) == 1
    assert sigs[0].side is Side.BUY
    assert sigs[0].strategy == "color_streak"


def test_no_setup_no_entry() -> None:
    s = ColorStreakStrategy(setup={})
    assert s.on_tick(_tick(120)) == []


def test_tp_exit() -> None:
    s = ColorStreakStrategy(setup={"005930": (100, 3)},
                            take_profit_pct=3.0, stop_loss_pct=2.0)
    s.on_tick(_tick(99))
    s.on_tick(_tick(102, ts_offset_min=1))  # entry @ 102
    sigs = s.on_tick(_tick(106, ts_offset_min=2))  # 102 * 1.03 = 105.06 → TP
    assert sigs and sigs[0].side is Side.SELL


def test_sl_exit() -> None:
    s = ColorStreakStrategy(setup={"005930": (100, 3)},
                            take_profit_pct=3.0, stop_loss_pct=2.0)
    s.on_tick(_tick(99))
    s.on_tick(_tick(102, ts_offset_min=1))
    sigs = s.on_tick(_tick(99, ts_offset_min=2))  # 102 * 0.98 = 99.96 → SL
    assert sigs and sigs[0].side is Side.SELL
    assert sigs[0].urgency == "high"


def test_no_double_entry() -> None:
    s = ColorStreakStrategy(setup={"005930": (100, 3)})
    s.on_tick(_tick(99))
    s.on_tick(_tick(102, ts_offset_min=1))  # entry
    s.on_tick(_tick(106, ts_offset_min=2))  # TP
    assert s.on_tick(_tick(105, ts_offset_min=3)) == []


def test_invalid_pct() -> None:
    with pytest.raises(ValueError):
        ColorStreakStrategy(setup={}, take_profit_pct=0)
