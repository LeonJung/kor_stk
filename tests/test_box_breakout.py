"""BoxBreakoutDetector tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.bus import EventBus
from ks_ws.detectors.box_breakout import BoxBreakoutDetector, detect_box_breakout
from ks_ws.domain import Bar
from ks_ws.events import BoxBreakoutDetected


def _bar(close: int, *, high: int | None = None, low: int | None = None,
         volume: int = 100, day: int = 1, sym: str = "X") -> Bar:
    high = high if high is not None else close + 5
    low = low if low is not None else close - 5
    return Bar(
        symbol=sym, timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day),
        timeframe="1d", open=close, high=high, low=low, close=close,
        volume=volume, value=close * volume,
    )


def _box_then_breakout(*, box_days: int = 10, box_close: int = 1000,
                        breakout_close: int = 1100, box_vol: int = 100,
                        breakout_vol: int = 300) -> list[Bar]:
    bars = [
        _bar(box_close, high=box_close + 10, low=box_close - 10, volume=box_vol, day=i + 1)
        for i in range(box_days)
    ]
    bars.append(_bar(breakout_close, high=breakout_close + 5, low=breakout_close - 10,
                     volume=breakout_vol, day=box_days + 1))
    return bars


def test_detect_clean_breakout() -> None:
    bars = _box_then_breakout()
    res = detect_box_breakout(bars)
    assert res is not None
    assert res.box_high == 1010
    assert res.box_low == 990
    assert res.breakout_price == 1100
    assert res.volume_multiplier == pytest.approx(3.0)


def test_no_breakout_when_close_inside_box() -> None:
    bars = _box_then_breakout(breakout_close=1005)
    assert detect_box_breakout(bars) is None


def test_no_breakout_low_volume() -> None:
    bars = _box_then_breakout(breakout_vol=150)  # only 1.5x
    assert detect_box_breakout(bars, volume_multiplier_min=2.0) is None


def test_no_breakout_box_too_wide() -> None:
    """박스권 폭 > box_range_pct → 패턴 아님."""
    bars = []
    for i in range(10):
        c = 1000 + (i % 2) * 80  # high range 8%
        bars.append(_bar(c, high=c + 5, low=c - 5, volume=100, day=i + 1))
    bars.append(_bar(1200, high=1205, low=1190, volume=300, day=11))
    assert detect_box_breakout(bars, box_range_pct=4.0) is None


def test_too_few_bars() -> None:
    bars = _box_then_breakout(box_days=3)
    # default box_days=10 but only 4 bars → None
    assert detect_box_breakout(bars) is None


def test_invalid_params() -> None:
    bars = _box_then_breakout()
    with pytest.raises(ValueError):
        detect_box_breakout(bars, box_days=0)
    with pytest.raises(ValueError):
        detect_box_breakout(bars, box_range_pct=-1)


def test_detector_emits_on_breakout() -> None:
    bus = EventBus()
    sub = bus.subscribe(BoxBreakoutDetected)
    det = BoxBreakoutDetector(bus)
    for b in _box_then_breakout():
        det.feed(b)
    events = []
    while sub.qsize() > 0:
        events.append(sub.get_nowait())
    assert len(events) == 1
    assert events[0].symbol == "X"
    assert events[0].box_high == 1010
    assert events[0].breakout_price == 1100


def test_detector_hysteresis_no_dup() -> None:
    """돌파 후 추가 위쪽 봉 들어와도 같은 box high 면 re-emit X."""
    bus = EventBus()
    sub = bus.subscribe(BoxBreakoutDetected)
    det = BoxBreakoutDetector(bus)
    bars = _box_then_breakout()
    for b in bars:
        det.feed(b)
    # additional above-breakout bars (each tries to form a new "box" but it
    # would have huge range — no new pattern)
    for d, c in enumerate((1110, 1120, 1130), start=12):
        det.feed(_bar(c, volume=100, day=d))
    events = []
    while sub.qsize() > 0:
        events.append(sub.get_nowait())
    assert len(events) == 1


def test_detector_re_emits_after_falling_back() -> None:
    """가격이 box high 아래로 돌아오면 hysteresis 해제 → 다음 box 형성 시 재 emit."""
    bus = EventBus()
    sub = bus.subscribe(BoxBreakoutDetected)
    det = BoxBreakoutDetector(bus)

    # First breakout
    for b in _box_then_breakout(box_close=1000, breakout_close=1100):
        det.feed(b)

    # Price falls back into old box (resets hysteresis)
    det.feed(_bar(1000, volume=100, day=12))

    # New box forms then breakout (different box_high)
    new_bars = _box_then_breakout(box_close=1200, breakout_close=1300)
    # Re-time the bars to days 13+
    for i, b in enumerate(new_bars):
        det.feed(Bar(
            symbol=b.symbol, timestamp=datetime(2026, 1, 13, tzinfo=UTC) + timedelta(days=i),
            timeframe=b.timeframe, open=b.open, high=b.high, low=b.low,
            close=b.close, volume=b.volume, value=b.value,
        ))

    events = []
    while sub.qsize() > 0:
        events.append(sub.get_nowait())
    assert len(events) == 2
    assert events[0].box_high == 1010
    assert events[1].box_high == 1210
