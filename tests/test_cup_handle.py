"""CupHandleDetector tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.bus import EventBus
from ks_ws.detectors.cup_handle import CupHandleDetector, detect_cup_handle
from ks_ws.domain import Bar
from ks_ws.events import CupHandleDetected


def _bar(close: int, *, high: int | None = None, low: int | None = None,
         volume: int = 100, day: int = 1, sym: str = "X") -> Bar:
    high = high if high is not None else close + 5
    low = low if low is not None else close - 5
    return Bar(
        symbol=sym, timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day),
        timeframe="1d", open=close, high=high, low=low, close=close,
        volume=volume, value=close * volume,
    )


def _cup_handle_bars() -> list[Bar]:
    """Cup and handle pattern:
    - days 1-3: left rim ~ 1100 (highs ~ 1105)
    - days 4-9: descent + U bottom ~ 900 (~ -18%)
    - days 10-13: ascent back to right rim ~ 1100 (~)
    - days 14-16: handle (highs ~ 1080, lows ~ 1050)
    - day 17: breakout close 1130
    """
    bars = []
    # Left rim
    for i, c in enumerate([1100, 1100, 1100], start=1):
        bars.append(_bar(c, high=c + 5, low=c - 5, day=i))
    # Descent into U
    for i, c in enumerate([1050, 1000, 950, 920, 905, 900], start=4):
        bars.append(_bar(c, high=c + 5, low=c - 5, day=i))
    # Ascent back to right rim (matching left ~ 1100)
    for i, c in enumerate([920, 970, 1020, 1080, 1100], start=10):
        bars.append(_bar(c, high=c + 5, low=c - 5, day=i))
    # Handle (small dip, high < right_rim 1105, low > cup_bottom 895)
    for i, c in enumerate([1080, 1060, 1075], start=15):
        bars.append(_bar(c, high=c + 5, low=c - 5, day=i))
    # Breakout: close above handle high (1085)
    bars.append(_bar(1130, high=1135, low=1110, day=18))
    return bars


def test_detect_clean_cup_handle() -> None:
    bars = _cup_handle_bars()
    res = detect_cup_handle(bars)
    assert res is not None
    assert res.cup_bottom <= 905
    assert res.cup_left_rim >= 1100
    assert res.cup_right_rim >= 1100
    assert res.breakout_price == 1130


def test_no_pattern_when_too_deep() -> None:
    """Cup depth > 30% → 패턴 아님."""
    bars = _cup_handle_bars()
    # Push U bottom to 500 (~ -55% depth)
    bars[8] = _bar(500, high=510, low=495, day=9)
    assert detect_cup_handle(bars, cup_depth_max_pct=30.0) is None


def test_no_pattern_when_no_breakout() -> None:
    bars = _cup_handle_bars()
    bars[-1] = _bar(1060, high=1070, low=1050, day=18)  # close below handle_high 1085
    assert detect_cup_handle(bars) is None


def test_no_pattern_when_asymmetric_rims() -> None:
    """좌우 림 ±5% 초과 → 패턴 아님."""
    bars = _cup_handle_bars()
    # Push right rim to 1300 (~ +20% vs left 1100)
    bars[13] = _bar(1300, high=1310, low=1290, day=14)
    assert detect_cup_handle(bars, cup_symmetry_pct=5.0) is None


def test_no_pattern_when_handle_too_deep() -> None:
    """Handle low ≤ cup_bottom → handle 가 cup 깊이 침범."""
    bars = _cup_handle_bars()
    # Drive handle to 800 (below cup_bottom 895)
    for d in (15, 16, 17):
        bars[d - 1] = _bar(800, high=810, low=795, day=d)
    assert detect_cup_handle(bars) is None


def test_too_few_bars() -> None:
    bars = [_bar(1000 + i * 10, day=i) for i in range(1, 6)]
    assert detect_cup_handle(bars) is None


def test_invalid_params() -> None:
    bars = _cup_handle_bars()
    with pytest.raises(ValueError):
        detect_cup_handle(bars, cup_depth_max_pct=0)
    with pytest.raises(ValueError):
        detect_cup_handle(bars, handle_days_min=5, handle_days_max=2)


def test_detector_emits() -> None:
    bus = EventBus()
    sub = bus.subscribe(CupHandleDetected)
    det = CupHandleDetector(bus)
    for b in _cup_handle_bars():
        det.feed(b)
    events = []
    while sub.qsize() > 0:
        events.append(sub.get_nowait())
    assert len(events) >= 1
    last = events[-1]
    assert last.cup_bottom <= 905
    assert last.breakout_price == 1130


def test_detector_hysteresis_no_dup_same_bottom() -> None:
    bus = EventBus()
    sub = bus.subscribe(CupHandleDetected)
    det = CupHandleDetector(bus)
    bars = _cup_handle_bars()
    for b in bars:
        det.feed(b)
    initial = det.emit_count
    # Extra bars above breakout — same cup_bottom hysteresis
    for d, c in zip([19, 20, 21], [1140, 1150, 1160], strict=True):
        det.feed(_bar(c, day=d))
    assert det.emit_count == initial
