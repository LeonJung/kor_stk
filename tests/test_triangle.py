"""TriangleDetector — 대칭/상승/하강 삼각수렴 tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.bus import EventBus
from ks_ws.detectors.triangle import TriangleDetector, detect_triangle
from ks_ws.domain import Bar
from ks_ws.events import TriangleDetected


def _bar(close: int, *, high: int | None = None, low: int | None = None,
         day: int = 1, sym: str = "X") -> Bar:
    high = high if high is not None else close + 5
    low = low if low is not None else close - 5
    return Bar(
        symbol=sym, timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day),
        timeframe="1d", open=close, high=high, low=low, close=close,
        volume=100, value=close * 100,
    )


def _ascending_bars() -> list[Bar]:
    """상승 삼각형: highs flat ~1100, lows up 950→1080. Breakout day 20: close 1120."""
    bars = []
    # first third (days 1-6): wide range, lows 950
    for i, c in enumerate([1080, 1050, 1020, 970, 950, 1000], start=1):
        bars.append(_bar(c, high=1105 if i == 1 else 1100, low=c - 5, day=i))
    # middle (days 7-13): tightening
    for i, c in enumerate([1020, 1050, 1070, 1040, 1060, 1080, 1070], start=7):
        bars.append(_bar(c, high=1100, low=c - 5, day=i))
    # last third (days 14-19): higher lows ~1060-1080, highs still ~1100
    for i, c in enumerate([1075, 1080, 1085, 1080, 1090, 1085], start=14):
        bars.append(_bar(c, high=1100, low=c - 5, day=i))
    # day 20: breakout (close > 1100)
    bars.append(_bar(1130, high=1140, low=1115, day=20))
    return bars


def _descending_bars() -> list[Bar]:
    """하강 삼각형: highs down 1100→1020, lows flat ~950. Breakout day 20 down: close 930."""
    bars = []
    # first third (days 1-6): highs 1100, lows around 950
    for i, c in enumerate([1080, 1050, 1020, 980, 960, 1000], start=1):
        bars.append(_bar(c, high=1105 if i == 1 else 1100, low=945, day=i))
    # middle (days 7-13)
    for i, c in enumerate([1010, 1040, 1050, 1030, 1010, 980, 990], start=7):
        bars.append(_bar(c, high=1060, low=945, day=i))
    # last third (days 14-19): highs ~1020-1030, lows still ~945
    for i, c in enumerate([1000, 990, 1010, 980, 990, 985], start=14):
        bars.append(_bar(c, high=1020, low=945, day=i))
    # day 20: breakout down (close < 945)
    bars.append(_bar(930, high=950, low=920, day=20))
    return bars


def _symmetrical_bars() -> list[Bar]:
    """대칭 삼각형: highs down 1150→1050, lows up 850→950. Breakout up day 20: 1080."""
    bars = []
    # first third: wide (highs 1150, lows 850)
    for i, c in enumerate([1100, 1050, 980, 900, 870, 950], start=1):
        bars.append(_bar(c, high=1150 if i == 1 else 1130, low=850 if i == 5 else c - 5, day=i))
    # middle
    for i, c in enumerate([970, 1000, 980, 1020, 990, 1010, 1005], start=7):
        bars.append(_bar(c, high=1080, low=920, day=i))
    # last third: highs ~1060, lows ~950 (converging)
    for i, c in enumerate([990, 1010, 1015, 1000, 1020, 1010], start=14):
        bars.append(_bar(c, high=1060, low=950, day=i))
    # day 20: breakout up (close > 1060)
    bars.append(_bar(1080, high=1090, low=1065, day=20))
    return bars


def test_detect_ascending_triangle() -> None:
    res = detect_triangle(_ascending_bars())
    assert res is not None
    assert res.variant == "ascending"
    assert res.direction == "up"
    assert res.breakout_price == 1130


def test_detect_descending_triangle() -> None:
    res = detect_triangle(_descending_bars())
    assert res is not None
    assert res.variant == "descending"
    assert res.direction == "down"


def test_detect_symmetrical_triangle() -> None:
    res = detect_triangle(_symmetrical_bars())
    assert res is not None
    assert res.variant == "symmetrical"


def test_no_pattern_when_no_breakout() -> None:
    bars = _ascending_bars()
    bars[-1] = _bar(1080, high=1095, low=1070, day=20)  # close inside apex
    assert detect_triangle(bars) is None


def test_no_pattern_when_expanding() -> None:
    """Highs up + lows down (expanding triangle) → not classified."""
    bars = []
    for i, c in enumerate([1000, 1010, 1020, 1030, 1015, 1020], start=1):
        bars.append(_bar(c, high=c + 5, low=c - 5, day=i))
    for i, c in enumerate([1040, 1030, 1050, 1035, 1060, 1040, 1080], start=7):
        bars.append(_bar(c, high=c + 10, low=c - 10, day=i))
    for i, c in enumerate([1100, 1050, 1110, 1040, 1120, 1030], start=14):
        bars.append(_bar(c, high=c + 20, low=c - 20, day=i))
    bars.append(_bar(1150, high=1170, low=990, day=20))
    assert detect_triangle(bars) is None


def test_too_few_bars() -> None:
    bars = [_bar(1000, day=i) for i in range(1, 5)]
    assert detect_triangle(bars) is None


def test_invalid_params() -> None:
    bars = _ascending_bars()
    with pytest.raises(ValueError):
        detect_triangle(bars, lookback=5)
    with pytest.raises(ValueError):
        detect_triangle(bars, trend_threshold_pct=0)


def test_detector_emits_on_breakout() -> None:
    bus = EventBus()
    sub = bus.subscribe(TriangleDetected)
    det = TriangleDetector(bus)
    for b in _ascending_bars():
        det.feed(b)
    events = []
    while sub.qsize() > 0:
        events.append(sub.get_nowait())
    assert len(events) >= 1
    last = events[-1]
    assert last.variant == "ascending"
    assert last.direction == "up"


def test_detector_hysteresis_no_dup() -> None:
    bus = EventBus()
    bus.subscribe(TriangleDetected)  # subscribe to drain queue, ignore handle
    det = TriangleDetector(bus)
    bars = _ascending_bars()
    for b in bars:
        det.feed(b)
    initial = det.emit_count
    # Extra bars above breakout — same signature hysteresis
    for d, c in zip([21, 22, 23], [1140, 1150, 1160], strict=True):
        det.feed(_bar(c, day=d))
    # No re-emit with same signature
    assert det.emit_count == initial
