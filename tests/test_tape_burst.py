"""TapeBurstStrategy — 분 단위 tick 수 폭증 시 BUY."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.domain import Side, Tick
from ks_ws.strategies.tape_burst import TapeBurstStrategy


def _tick(price: int, *, sec_offset: int = 0,
          sym: str = "005930") -> Tick:
    base = datetime(2026, 5, 13, 0, 0, 0, tzinfo=UTC)
    return Tick(
        symbol=sym, price=price, volume=100,
        timestamp=base + timedelta(seconds=sec_offset),
    )


def _seed_baseline(s: TapeBurstStrategy, *, minutes: int, per_minute: int) -> None:
    """Generate per_minute ticks per minute for `minutes` minutes."""
    sec = 0
    for m in range(minutes):
        for _ in range(per_minute):
            s.on_tick(_tick(100, sec_offset=sec))
            sec += 1
        # advance to next minute boundary
        sec = (m + 1) * 60


def test_no_burst_in_normal_flow() -> None:
    s = TapeBurstStrategy(
        baseline_minutes=10, burst_ratio=3.0, min_baseline_count=10,
    )
    _seed_baseline(s, minutes=12, per_minute=15)
    # No burst signal
    state = s._state["005930"]
    assert len(s.open_positions()) == 0
    assert state.current_count <= 15


def test_burst_triggers_entry() -> None:
    s = TapeBurstStrategy(
        baseline_minutes=5, burst_ratio=3.0, min_baseline_count=5,
    )
    # 5 baseline minutes x 10 tick each = baseline avg 10
    sec = 0
    for m in range(5):
        for _ in range(10):
            s.on_tick(_tick(100, sec_offset=sec))
            sec += 1
        sec = (m + 1) * 60
    # 6th minute: 50 ticks (5x baseline) — must transition to minute 7 to trigger
    for _ in range(50):
        s.on_tick(_tick(100, sec_offset=sec))
        sec += 1
    # 7th minute starts — closes the burst minute. First tick should trigger.
    sec = 6 * 60
    sigs = s.on_tick(_tick(105, sec_offset=sec))
    assert sigs and sigs[0].side is Side.BUY
    assert sigs[0].strategy == "tape_burst"


def test_no_burst_below_min_baseline() -> None:
    s = TapeBurstStrategy(
        baseline_minutes=5, burst_ratio=3.0, min_baseline_count=20,  # high threshold
    )
    # Baseline minutes only have 5 tick each — below min_baseline 20
    sec = 0
    for m in range(5):
        for _ in range(5):
            s.on_tick(_tick(100, sec_offset=sec))
            sec += 1
        sec = (m + 1) * 60
    # Burst minute
    for _ in range(50):
        s.on_tick(_tick(100, sec_offset=sec))
        sec += 1
    sec = 6 * 60
    sigs = s.on_tick(_tick(105, sec_offset=sec))
    # baseline too low → no burst
    assert all(sig.side is not Side.BUY for sig in sigs)


def test_tp_exit() -> None:
    s = TapeBurstStrategy(
        baseline_minutes=5, burst_ratio=3.0, min_baseline_count=5,
        take_profit_pct=1.5, stop_loss_pct=1.0,
    )
    # Build up baseline and burst as before
    sec = 0
    for m in range(5):
        for _ in range(10):
            s.on_tick(_tick(100, sec_offset=sec))
            sec += 1
        sec = (m + 1) * 60
    for _ in range(50):
        s.on_tick(_tick(100, sec_offset=sec))
        sec += 1
    sec = 6 * 60
    s.on_tick(_tick(105, sec_offset=sec))  # entry @ 105
    # TP = 105 * 1.015 = 106.575 → 107 triggers
    sigs = s.on_tick(_tick(107, sec_offset=sec + 30))
    assert sigs and sigs[0].side is Side.SELL


def test_invalid_baseline_raises() -> None:
    with pytest.raises(ValueError):
        TapeBurstStrategy(baseline_minutes=2)
    with pytest.raises(ValueError):
        TapeBurstStrategy(burst_ratio=1.0)


def test_invalid_pct_raises() -> None:
    with pytest.raises(ValueError):
        TapeBurstStrategy(take_profit_pct=0)


def test_no_double_entry_same_day() -> None:
    s = TapeBurstStrategy(
        baseline_minutes=5, burst_ratio=3.0, min_baseline_count=5,
    )
    # Trigger burst
    sec = 0
    for m in range(5):
        for _ in range(10):
            s.on_tick(_tick(100, sec_offset=sec))
            sec += 1
        sec = (m + 1) * 60
    for _ in range(50):
        s.on_tick(_tick(100, sec_offset=sec))
        sec += 1
    sec = 6 * 60
    s.on_tick(_tick(105, sec_offset=sec))  # entry
    # Exit via TP
    s.on_tick(_tick(107, sec_offset=sec + 30))
    # New burst attempt — should not re-enter same day
    sec_new = 7 * 60
    for _ in range(50):
        s.on_tick(_tick(105, sec_offset=sec_new))
        sec_new += 1
    sec_new = 8 * 60
    sigs = s.on_tick(_tick(110, sec_offset=sec_new))
    assert all(sig.side is not Side.BUY for sig in sigs)
