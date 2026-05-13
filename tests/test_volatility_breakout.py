"""VolatilityBreakoutStrategy — Larry Williams 변동성 돌파."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.domain import Side, Tick
from ks_ws.strategies.volatility_breakout import (
    VolatilityBreakoutStrategy,
    compute_prev_high_low,
)

_KST_HOUR = 0  # UTC 00:00 = KST 09:00


def _tick(price: int, *, ts_offset_min: int = 0,
          sym: str = "005930") -> Tick:
    base = datetime(2026, 5, 13, _KST_HOUR, 0, tzinfo=UTC)
    return Tick(
        symbol=sym, price=price, volume=100,
        timestamp=base + timedelta(minutes=ts_offset_min),
    )


def test_entry_on_trigger_cross() -> None:
    # prev: H=110, L=90, range=20. k=0.5 → trigger offset = 10.
    # day open = 100 → trigger = 110.
    s = VolatilityBreakoutStrategy(
        prev_high_low={"005930": (110, 90)},
        k=0.5, take_profit_pct=3.0, stop_loss_pct=2.0,
    )
    # First tick = day open at 100, no entry
    assert s.on_tick(_tick(100)) == []
    # Stay below trigger
    assert s.on_tick(_tick(105, ts_offset_min=1)) == []
    # Cross trigger (110)
    sigs = s.on_tick(_tick(112, ts_offset_min=2))
    assert len(sigs) == 1
    assert sigs[0].side is Side.BUY
    assert sigs[0].strategy == "volatility_breakout"


def test_no_double_entry_same_day() -> None:
    s = VolatilityBreakoutStrategy(prev_high_low={"005930": (110, 90)})
    s.on_tick(_tick(100))
    s.on_tick(_tick(112, ts_offset_min=1))  # entry
    # TP exit
    s.on_tick(_tick(116, ts_offset_min=2))  # 112 * 1.03 = 115.36 → TP
    # Same-day attempt to re-cross
    assert s.on_tick(_tick(116, ts_offset_min=3)) == []


def test_tp_exit() -> None:
    s = VolatilityBreakoutStrategy(
        prev_high_low={"005930": (110, 90)},
        k=0.5, take_profit_pct=3.0, stop_loss_pct=2.0,
    )
    s.on_tick(_tick(100))
    s.on_tick(_tick(112, ts_offset_min=1))  # entry at 112
    sigs = s.on_tick(_tick(116, ts_offset_min=2))  # 112 * 1.03 = 115.36 < 116 → TP
    assert sigs and sigs[0].side is Side.SELL


def test_sl_exit() -> None:
    s = VolatilityBreakoutStrategy(
        prev_high_low={"005930": (110, 90)},
        k=0.5, take_profit_pct=3.0, stop_loss_pct=2.0,
    )
    s.on_tick(_tick(100))
    s.on_tick(_tick(112, ts_offset_min=1))
    # SL = 112 * 0.98 = 109.76
    sigs = s.on_tick(_tick(109, ts_offset_min=2))
    assert sigs and sigs[0].side is Side.SELL
    assert sigs[0].urgency == "high"


def test_no_entry_below_trigger() -> None:
    s = VolatilityBreakoutStrategy(prev_high_low={"005930": (110, 90)})
    s.on_tick(_tick(100))
    assert s.on_tick(_tick(109, ts_offset_min=1)) == []  # 109 < 110


def test_edge_detection_avoids_repeat() -> None:
    """Tick already above trigger → no new entry on next tick still above."""
    s = VolatilityBreakoutStrategy(prev_high_low={"005930": (110, 90)})
    s.on_tick(_tick(100))
    sigs1 = s.on_tick(_tick(112, ts_offset_min=1))  # cross → entry
    assert len(sigs1) == 1
    # 다음 tick 도 위 — 진입 후 was_above remains True → no new entry signal
    # (이미 _open 에 들어가 있어서 entry check 자체 안 일어남)
    sigs2 = s.on_tick(_tick(113, ts_offset_min=2))
    assert all(s.side is not Side.BUY for s in sigs2)


def test_missing_prev_hl_no_entry() -> None:
    s = VolatilityBreakoutStrategy(prev_high_low={})  # 005930 없음
    s.on_tick(_tick(100))
    assert s.on_tick(_tick(200, ts_offset_min=1)) == []


def test_invalid_k_raises() -> None:
    with pytest.raises(ValueError):
        VolatilityBreakoutStrategy(prev_high_low={}, k=0)
    with pytest.raises(ValueError):
        VolatilityBreakoutStrategy(prev_high_low={}, k=2.0)


def test_invalid_pct_raises() -> None:
    with pytest.raises(ValueError):
        VolatilityBreakoutStrategy(prev_high_low={}, take_profit_pct=0)


def test_compute_prev_high_low_from_bar_store(tmp_path) -> None:
    from ks_ws.domain import Bar
    from ks_ws.storage.bars import BarStore
    store = BarStore(tmp_path)
    store.write([
        Bar(symbol="005930", timeframe="1d",
            timestamp=datetime(2026, 5, 12, tzinfo=UTC),
            open=100, high=110, low=90, close=105,
            volume=1_000, value=105_000),
    ])
    out = compute_prev_high_low(store, ["005930"])
    assert out["005930"] == (110, 90)


def test_zero_range_skips_entry() -> None:
    s = VolatilityBreakoutStrategy(prev_high_low={"005930": (100, 100)})
    s.on_tick(_tick(100))
    assert s.on_tick(_tick(200, ts_offset_min=1)) == []  # range=0 → no trigger
