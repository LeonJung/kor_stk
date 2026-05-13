"""DoubleBottomDetector — W 패턴 탐지."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.bus import EventBus
from ks_ws.detectors.double_bottom import (
    DoubleBottomDetected,
    DoubleBottomDetector,
    detect_double_bottom,
)
from ks_ws.domain import Bar


def _make_bar(close: int, *, low: int | None = None, high: int | None = None,
              day: int = 1, symbol: str = "X") -> Bar:
    low = low if low is not None else close - 50
    high = high if high is not None else close + 50
    return Bar(
        symbol=symbol,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day),
        timeframe="1d",
        open=close,
        high=high,
        low=low,
        close=close,
        volume=100,
        value=close * 100,
    )


def _w_pattern_bars() -> list[Bar]:
    """Construct a clean W pattern:
    - day 1-5: decline 1100→1000 (low1 at day 5, low=1000)
    - day 6-10: rebound 1000→1080 (neckline ~1080)
    - day 11-15: decline 1080→1010 (low2 at day 15, low=1010 ≈ low1 +1%)
    - day 16: breakout close above neckline (close=1100)
    """
    bars = []
    # Decline to low1
    for i, c in enumerate([1100, 1080, 1050, 1020, 1000], start=1):
        bars.append(_make_bar(c, low=c - 5, high=c + 10, day=i))
    # Rebound to neckline ~ 1080
    for i, c in enumerate([1020, 1050, 1070, 1075, 1080], start=6):
        bars.append(_make_bar(c, low=c - 10, high=c + 5, day=i))
    # Decline to low2 (within ±2% of low1=1000 → 980~1020)
    for i, c in enumerate([1070, 1050, 1030, 1020, 1010], start=11):
        bars.append(_make_bar(c, low=c - 5, high=c + 10, day=i))
    # Breakout: close above neckline 1080
    bars.append(_make_bar(1100, low=1080, high=1110, day=16))
    return bars


def test_detect_clean_w_pattern() -> None:
    res = detect_double_bottom(_w_pattern_bars(), lookback=60)
    assert res is not None
    # Deepest valid pair: day 5 low=995 / day 15 low=1005 (within tolerance)
    assert 990 <= res.low1_price <= 1000
    assert 1000 <= res.low2_price <= 1015
    assert res.neckline_price >= 1080
    # measured target = neckline + (neckline - min_low)
    assert res.target_price >= res.neckline_price


def test_no_pattern_when_not_broken_out() -> None:
    bars = _w_pattern_bars()
    # Replace last (breakout) bar with one that closes below neckline
    last = bars[-1]
    bars[-1] = Bar(
        symbol=last.symbol,
        timestamp=last.timestamp,
        timeframe=last.timeframe,
        open=1050,
        high=1075,
        low=1040,
        close=1050,  # below neckline 1080
        volume=100,
        value=100_000,
    )
    res = detect_double_bottom(bars)
    assert res is None


def test_no_pattern_when_low2_too_far_from_low1() -> None:
    bars = _w_pattern_bars()
    # Push the whole second-decline window way below low1 → no valid W (low2
    # within tolerance of low1) and the would-be neckline rise also small.
    for d_idx, c in enumerate([850, 855, 850, 845, 840]):
        bars[10 + d_idx] = _make_bar(c, low=c - 3, high=c + 3, day=11 + d_idx)
    bars[-1] = _make_bar(860, low=855, high=865, day=16)  # no breakout above 1080
    res = detect_double_bottom(bars, low_tolerance_pct=2.0)
    assert res is None


def test_no_pattern_when_separation_too_small() -> None:
    """Same close-by lows but neckline window < min_separation_bars → no pattern."""
    bars = []
    bars.append(_make_bar(1000, low=1000, high=1010, day=1))  # low1
    bars.append(_make_bar(1010, low=1010, high=1015, day=2))  # not enough separation
    bars.append(_make_bar(1005, low=1000, high=1020, day=3))  # low2 too close
    bars.append(_make_bar(1100, low=1080, high=1110, day=4))  # 'breakout'
    res = detect_double_bottom(bars, min_separation_bars=5, max_separation_bars=30)
    assert res is None


def test_neckline_min_rise_filter() -> None:
    """Flat W (lows close, but rise between them <3%) → no pattern."""
    bars = _w_pattern_bars()
    # Flatten the rebound peak from 1080 to 1015 (only 1.5% rise from low1=1000)
    for i in (5, 6, 7, 8, 9):  # bar indices for rebound
        bars[i] = _make_bar(1010, low=1005, high=1015, day=i + 1)
    bars[-1] = _make_bar(1020, low=1015, high=1025, day=16)  # would-be breakout
    res = detect_double_bottom(bars, neckline_min_rise_pct=3.0)
    assert res is None


def test_too_few_bars() -> None:
    bars = [_make_bar(1000, day=i) for i in range(1, 4)]
    assert detect_double_bottom(bars) is None


def test_invalid_tolerance() -> None:
    with pytest.raises(ValueError):
        detect_double_bottom([_make_bar(1000, day=1)], low_tolerance_pct=-1)


def test_invalid_separation_range() -> None:
    with pytest.raises(ValueError):
        detect_double_bottom([_make_bar(1000, day=1)] * 10,
                             min_separation_bars=30, max_separation_bars=5)


# --- stateful DoubleBottomDetector ---


def test_detector_emits_on_pattern_completion() -> None:
    bus = EventBus()
    sub = bus.subscribe(DoubleBottomDetected)
    det = DoubleBottomDetector(bus)
    for bar in _w_pattern_bars():
        det.feed(bar)

    events = []
    while sub.qsize() > 0:
        events.append(sub.get_nowait())
    assert len(events) == 1
    assert events[0].symbol == "X"
    assert 990 <= events[0].low1_price <= 1000
    assert events[0].neckline_price >= 1080


def test_detector_hysteresis_no_dup() -> None:
    """Feeding same pattern + extra above-neckline bars → only one emit."""
    bus = EventBus()
    sub = bus.subscribe(DoubleBottomDetected)
    det = DoubleBottomDetector(bus)
    bars = _w_pattern_bars()
    for bar in bars:
        det.feed(bar)
    # Add a few more above-neckline bars
    last_day = 17
    for c in (1110, 1120, 1130):
        det.feed(_make_bar(c, low=c - 10, high=c + 10, day=last_day))
        last_day += 1

    events = []
    while sub.qsize() > 0:
        events.append(sub.get_nowait())
    assert len(events) == 1  # only the original breakout emit
