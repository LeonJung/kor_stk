"""HeadShouldersDetector (역H&S 위주) tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.bus import EventBus
from ks_ws.detectors.head_shoulders import (
    HeadShouldersDetector,
    detect_inverse_head_shoulders,
)
from ks_ws.domain import Bar
from ks_ws.events import HeadShouldersDetected


def _bar(close: int, *, high: int | None = None, low: int | None = None,
         day: int = 1, sym: str = "X") -> Bar:
    high = high if high is not None else close + 5
    low = low if low is not None else close - 5
    return Bar(
        symbol=sym, timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day),
        timeframe="1d", open=close, high=high, low=low, close=close,
        volume=100, value=close * 100,
    )


def _inverse_hns_bars() -> list[Bar]:
    """역H&S pattern bars.
    - day 1-5: 하락 → left shoulder low ~ 980 (day 5)
    - day 6-9: 반등 peak ~ 1050 (day 8)
    - day 10-13: 하락 head ~ 950 (day 12 = head, 가장 깊음)
    - day 14-17: 반등 peak ~ 1050 (day 16)
    - day 18-21: 하락 right shoulder ~ 985 (day 20)
    - day 22: 반등 close > neckline (1100)
    """
    bars = []
    # Decline to left shoulder
    closes_l = [1050, 1020, 1000, 990, 980]
    for i, c in enumerate(closes_l, start=1):
        bars.append(_bar(c, high=c + 5, low=c - 5, day=i))
    # Rebound peak left
    closes_pl = [1010, 1030, 1050, 1040]
    for i, c in enumerate(closes_pl, start=6):
        bars.append(_bar(c, high=c + 5, low=c - 5, day=i))
    # Decline to head (deepest)
    closes_h = [1000, 970, 950, 970]
    for i, c in enumerate(closes_h, start=10):
        bars.append(_bar(c, high=c + 5, low=c - 5, day=i))
    # Rebound peak right
    closes_pr = [1000, 1030, 1050, 1040]
    for i, c in enumerate(closes_pr, start=14):
        bars.append(_bar(c, high=c + 5, low=c - 5, day=i))
    # Decline to right shoulder (~ left shoulder)
    closes_r = [1010, 1000, 990, 985]
    for i, c in enumerate(closes_r, start=18):
        bars.append(_bar(c, high=c + 5, low=c - 5, day=i))
    # Breakout: close above neckline (peaks ~ 1055)
    bars.append(_bar(1100, high=1105, low=1080, day=22))
    return bars


def test_detect_inverse_hns_clean() -> None:
    bars = _inverse_hns_bars()
    res = detect_inverse_head_shoulders(bars)
    assert res is not None
    # head price = lowest low (945) — day 12 close 950 low 945
    assert res.head_price <= 950
    # left/right shoulder roughly similar
    assert abs(res.left_shoulder_price - res.right_shoulder_price) <= 50
    # neckline above shoulders
    assert res.neckline_price > max(res.left_shoulder_price, res.right_shoulder_price)


def test_no_pattern_when_no_breakout() -> None:
    bars = _inverse_hns_bars()
    # Replace last bar to close below neckline
    last = bars[-1]
    bars[-1] = Bar(
        symbol=last.symbol, timestamp=last.timestamp, timeframe=last.timeframe,
        open=1000, high=1010, low=980, close=1000, volume=100, value=100_000,
    )
    assert detect_inverse_head_shoulders(bars) is None


def test_no_pattern_when_shoulders_unequal() -> None:
    bars = _inverse_hns_bars()
    # Push right shoulder way down (asymmetric)
    bars[19] = _bar(700, high=720, low=695, day=20)  # right_shoulder low far below left
    assert detect_inverse_head_shoulders(bars, shoulder_tolerance_pct=3.0) is None


def test_no_pattern_when_head_not_deep_enough() -> None:
    """Head ≈ shoulders → no clear head."""
    bars = _inverse_hns_bars()
    bars[11] = _bar(985, high=990, low=980, day=12)  # head not deeper than shoulders
    assert detect_inverse_head_shoulders(bars, head_depth_min_pct=2.0) is None


def test_too_few_bars() -> None:
    bars = [_bar(1000, day=i) for i in range(1, 5)]
    assert detect_inverse_head_shoulders(bars) is None


def test_invalid_params() -> None:
    bars = _inverse_hns_bars()
    with pytest.raises(ValueError):
        detect_inverse_head_shoulders(bars, shoulder_tolerance_pct=-1)
    with pytest.raises(ValueError):
        detect_inverse_head_shoulders(bars, min_gap_bars=0)


def test_detector_emits_on_pattern() -> None:
    bus = EventBus()
    sub = bus.subscribe(HeadShouldersDetected)
    det = HeadShouldersDetector(bus)
    for b in _inverse_hns_bars():
        det.feed(b)
    events = []
    while sub.qsize() > 0:
        events.append(sub.get_nowait())
    assert len(events) == 1
    assert events[0].pattern == "inverse_head_shoulders"


def test_detector_hysteresis_no_dup() -> None:
    """같은 head 의 패턴 — 후속 bars 추가해도 re-emit X."""
    bus = EventBus()
    sub = bus.subscribe(HeadShouldersDetected)
    det = HeadShouldersDetector(bus)
    bars = _inverse_hns_bars()
    for b in bars:
        det.feed(b)
    # extra above-neckline bars
    for d, c in zip([23, 24, 25], [1110, 1120, 1130], strict=True):
        det.feed(_bar(c, day=d))
    events = []
    while sub.qsize() > 0:
        events.append(sub.get_nowait())
    assert len(events) == 1
