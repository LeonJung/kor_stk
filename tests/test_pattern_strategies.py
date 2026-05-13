"""pattern_strategies — DoubleBottom / BoxBreakout / InverseHnS strategies."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.domain import Side, Tick
from ks_ws.events import (
    BoxBreakoutDetected,
    DoubleBottomDetected,
    HeadShouldersDetected,
)
from ks_ws.strategies.pattern_strategies import (
    BoxBreakoutStrategy,
    DoubleBottomStrategy,
    InverseHeadShouldersStrategy,
)


_BASE = datetime(2026, 5, 13, 9, 0, tzinfo=UTC)


def _tick(price: int, *, ts_offset_min: int = 0, sym: str = "005930") -> Tick:
    return Tick(
        symbol=sym, price=price, volume=100,
        timestamp=_BASE + timedelta(minutes=ts_offset_min),
    )


# --- DoubleBottomStrategy ---


def test_double_bottom_entry_on_event() -> None:
    s = DoubleBottomStrategy(take_profit_pct=3.0, stop_loss_pct=2.0)
    ev = DoubleBottomDetected(
        symbol="005930", timestamp=_BASE,
        low1_price=1000, low2_price=1005, neckline_price=1100, target_price=1200,
    )
    sigs = s.on_event(ev)
    assert len(sigs) == 1
    assert sigs[0].side is Side.BUY
    assert sigs[0].strategy == "double_bottom"


def test_double_bottom_same_day_no_dup() -> None:
    s = DoubleBottomStrategy()
    ev = DoubleBottomDetected(
        symbol="005930", timestamp=_BASE,
        low1_price=1000, low2_price=1005, neckline_price=1100, target_price=1200,
    )
    s.on_event(ev)
    sigs = s.on_event(ev)  # same-day duplicate event
    assert sigs == []


def test_double_bottom_tp_exit() -> None:
    s = DoubleBottomStrategy(take_profit_pct=3.0, stop_loss_pct=2.0)
    s.on_event(DoubleBottomDetected(
        symbol="005930", timestamp=_BASE,
        low1_price=1000, low2_price=1005, neckline_price=1100, target_price=1200,
    ))
    # Tick price at +3% → TP
    sigs = s.on_tick(_tick(1133))  # 1100 * 1.03 = 1133
    assert sigs and sigs[0].side is Side.SELL


def test_double_bottom_sl_exit() -> None:
    s = DoubleBottomStrategy(take_profit_pct=3.0, stop_loss_pct=2.0)
    s.on_event(DoubleBottomDetected(
        symbol="005930", timestamp=_BASE,
        low1_price=1000, low2_price=1005, neckline_price=1100, target_price=1200,
    ))
    sigs = s.on_tick(_tick(1077))  # 1100 * 0.98 = 1078, 1077 below
    assert sigs and sigs[0].side is Side.SELL
    assert sigs[0].urgency == "high"


def test_double_bottom_hold_timeout() -> None:
    s = DoubleBottomStrategy(max_hold_minutes=10)
    s.on_event(DoubleBottomDetected(
        symbol="005930", timestamp=_BASE,
        low1_price=1000, low2_price=1005, neckline_price=1100, target_price=1200,
    ))
    sigs = s.on_tick(_tick(1100, ts_offset_min=11))
    assert sigs and sigs[0].side is Side.SELL


def test_double_bottom_ignores_other_events() -> None:
    s = DoubleBottomStrategy()
    ev = BoxBreakoutDetected(
        symbol="005930", timestamp=_BASE,
        box_high=1010, box_low=990, box_days=10,
        breakout_price=1100, volume_multiplier=3.0,
    )
    assert s.on_event(ev) == []


# --- BoxBreakoutStrategy ---


def test_box_breakout_entry_on_event() -> None:
    s = BoxBreakoutStrategy()
    ev = BoxBreakoutDetected(
        symbol="005930", timestamp=_BASE,
        box_high=1010, box_low=990, box_days=10,
        breakout_price=1100, volume_multiplier=3.0,
    )
    sigs = s.on_event(ev)
    assert len(sigs) == 1
    assert sigs[0].side is Side.BUY
    assert sigs[0].strategy == "box_breakout"
    assert "vol*3.0" in (sigs[0].note or "")


def test_box_breakout_ignores_other_events() -> None:
    s = BoxBreakoutStrategy()
    ev = DoubleBottomDetected(
        symbol="005930", timestamp=_BASE,
        low1_price=1000, low2_price=1005, neckline_price=1100, target_price=1200,
    )
    assert s.on_event(ev) == []


# --- InverseHeadShouldersStrategy ---


def test_inverse_hns_entry_on_event() -> None:
    s = InverseHeadShouldersStrategy()
    ev = HeadShouldersDetected(
        symbol="005930", timestamp=_BASE, pattern="inverse_head_shoulders",
        left_shoulder_price=980, head_price=950, right_shoulder_price=985,
        neckline_price=1050, target_price=1150,
    )
    sigs = s.on_event(ev)
    assert len(sigs) == 1
    assert sigs[0].side is Side.BUY
    assert sigs[0].strategy == "inverse_head_shoulders"


def test_inverse_hns_skips_bearish_pattern() -> None:
    s = InverseHeadShouldersStrategy()
    ev = HeadShouldersDetected(
        symbol="005930", timestamp=_BASE, pattern="head_shoulders",  # bearish
        left_shoulder_price=1100, head_price=1200, right_shoulder_price=1110,
        neckline_price=1050, target_price=900,
    )
    assert s.on_event(ev) == []


# --- shared ---


def test_invalid_pct() -> None:
    with pytest.raises(ValueError):
        DoubleBottomStrategy(take_profit_pct=-1.0)
    with pytest.raises(ValueError):
        BoxBreakoutStrategy(stop_loss_pct=0)
    with pytest.raises(ValueError):
        InverseHeadShouldersStrategy(confidence=1.5)


def test_open_positions_isolation() -> None:
    """다른 strategy 의 entry 가 같은 종목 entry 차단 X (각각 독립 _open)."""
    s1 = DoubleBottomStrategy()
    s2 = BoxBreakoutStrategy()
    s1.on_event(DoubleBottomDetected(
        symbol="005930", timestamp=_BASE,
        low1_price=1000, low2_price=1005, neckline_price=1100, target_price=1200,
    ))
    sigs = s2.on_event(BoxBreakoutDetected(
        symbol="005930", timestamp=_BASE,
        box_high=1010, box_low=990, box_days=10,
        breakout_price=1100, volume_multiplier=3.0,
    ))
    assert len(sigs) == 1  # box_breakout 도 같은 종목 entry 가능
