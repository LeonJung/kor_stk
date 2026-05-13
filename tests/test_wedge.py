"""WedgeDetector tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.bus import EventBus
from ks_ws.detectors.wedge import WedgeDetected, WedgeDetector, detect_wedge
from ks_ws.domain import Bar


def _bar(close: int, *, high: int | None = None, low: int | None = None,
         day: int = 1, sym: str = "X") -> Bar:
    high = high if high is not None else close + 5
    low = low if low is not None else close - 5
    return Bar(
        symbol=sym, timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day),
        timeframe="1d", open=close, high=high, low=low, close=close,
        volume=100, value=close * 100,
    )


def _falling_wedge_bars() -> list[Bar]:
    """Falling wedge: highs down 1200→1080 (-10%), lows down 1000→950 (-5%).
    upper slope > lower slope (dominance). Breakout up: day 20 close 1130."""
    bars = []
    # first third (days 1-6): highs ~1200, lows ~1000
    for i, c in enumerate([1100, 1080, 1180, 1010, 1190, 1050], start=1):
        bars.append(_bar(c, high=1200, low=1000, day=i))
    # middle (days 7-13)
    for i, c in enumerate([1100, 1080, 1120, 1050, 1080, 1040, 1070], start=7):
        bars.append(_bar(c, high=1150, low=980, day=i))
    # last third (days 14-19): highs ~1080, lows ~950 (upper dropped ~10%, lower ~5%)
    for i, c in enumerate([1050, 1020, 1060, 1000, 1080, 1020], start=14):
        bars.append(_bar(c, high=1080, low=950, day=i))
    # day 20: breakout up (close > 1080)
    bars.append(_bar(1130, high=1140, low=1100, day=20))
    return bars


def _rising_wedge_bars() -> list[Bar]:
    """Rising wedge: highs up 1100→1180 (+7%), lows up 950→1100 (+15%).
    lower slope > upper slope (dominance). Breakout down: day 20 close 1080."""
    bars = []
    for i, c in enumerate([1020, 1060, 980, 1080, 1000, 1070], start=1):
        bars.append(_bar(c, high=1100, low=950, day=i))
    for i, c in enumerate([1050, 1080, 1060, 1100, 1080, 1110, 1090], start=7):
        bars.append(_bar(c, high=1140, low=1000, day=i))
    for i, c in enumerate([1130, 1140, 1150, 1130, 1170, 1140], start=14):
        bars.append(_bar(c, high=1180, low=1100, day=i))
    # day 20: breakout down (close < 1100)
    bars.append(_bar(1080, high=1115, low=1070, day=20))
    return bars


def test_detect_falling_wedge_breaks_up() -> None:
    res = detect_wedge(_falling_wedge_bars())
    assert res is not None
    assert res.wedge_type == "falling"
    assert res.direction == "up"
    assert res.breakout_price == 1130


def test_detect_rising_wedge_breaks_down() -> None:
    res = detect_wedge(_rising_wedge_bars())
    assert res is not None
    assert res.wedge_type == "rising"
    assert res.direction == "down"
    assert res.breakout_price == 1080


def test_no_pattern_when_no_breakout() -> None:
    bars = _falling_wedge_bars()
    bars[-1] = _bar(1050, high=1075, low=1020, day=20)  # close inside upper_last
    assert detect_wedge(bars) is None


def test_no_pattern_when_slopes_too_small() -> None:
    """Highs/lows nearly flat → min_slope_pct unmet."""
    bars = []
    for i, c in enumerate([1100, 1095, 1100, 1090, 1100, 1095], start=1):
        bars.append(_bar(c, high=1110, low=1080, day=i))
    for i, c in enumerate([1095, 1100, 1095, 1100, 1095, 1100, 1095], start=7):
        bars.append(_bar(c, high=1105, low=1085, day=i))
    for i, c in enumerate([1090, 1095, 1090, 1095, 1090, 1095], start=14):
        bars.append(_bar(c, high=1100, low=1080, day=i))
    bars.append(_bar(1110, high=1115, low=1095, day=20))
    assert detect_wedge(bars, min_slope_pct=3.0) is None


def test_no_pattern_when_dominance_fails() -> None:
    """Both slopes negative but upper ≈ lower → no clear wedge dominance."""
    bars = []
    # first: highs 1200, lows 1000
    for i in range(1, 7):
        bars.append(_bar(1100, high=1200, low=1000, day=i))
    for i in range(7, 14):
        bars.append(_bar(1090, high=1180, low=990, day=i))
    # last: highs 1100 (-8%), lows 920 (-8%) → similar slopes → no dominance
    for i in range(14, 20):
        bars.append(_bar(1010, high=1100, low=920, day=i))
    bars.append(_bar(1130, high=1140, low=1100, day=20))
    assert detect_wedge(bars, slope_dominance_ratio=1.3) is None


def test_too_few_bars() -> None:
    bars = [_bar(1000, day=i) for i in range(1, 5)]
    assert detect_wedge(bars) is None


def test_invalid_params() -> None:
    bars = _falling_wedge_bars()
    with pytest.raises(ValueError):
        detect_wedge(bars, lookback=5)
    with pytest.raises(ValueError):
        detect_wedge(bars, min_slope_pct=0)


def test_detector_emits_on_breakout() -> None:
    bus = EventBus()
    sub = bus.subscribe(WedgeDetected)
    det = WedgeDetector(bus)
    for b in _falling_wedge_bars():
        det.feed(b)
    events = []
    while sub.qsize() > 0:
        events.append(sub.get_nowait())
    assert len(events) >= 1
    last = events[-1]
    assert last.wedge_type == "falling"
    assert last.direction == "up"


def test_detector_hysteresis_no_dup() -> None:
    """Additional same-signature bars produce no new emits. (Different
    upper_last after sliding may still emit — that's a new wedge instance.)"""
    bus = EventBus()
    bus.subscribe(WedgeDetected)  # drain
    det = WedgeDetector(bus)
    bars = _falling_wedge_bars()
    for b in bars:
        det.feed(b)
    initial = det.emit_count
    # Re-feed last bar (same signature) → no new emit
    det.feed(_bar(1135, day=20))
    assert det.emit_count == initial
