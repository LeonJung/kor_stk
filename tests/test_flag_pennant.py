"""FlagPennantDetector — 깃발 / 페넌트 패턴 tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.bus import EventBus
from ks_ws.detectors.flag_pennant import FlagPennantDetector, detect_flag_breakout
from ks_ws.domain import Bar
from ks_ws.events import FlagPennantDetected


def _bar(close: int, *, high: int | None = None, low: int | None = None,
         volume: int = 100, day: int = 1, sym: str = "X") -> Bar:
    high = high if high is not None else close + 5
    low = low if low is not None else close - 5
    return Bar(
        symbol=sym, timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day),
        timeframe="1d", open=close, high=high, low=low, close=close,
        volume=volume, value=close * volume,
    )


def _bullish_flag_bars() -> list[Bar]:
    """깃대 5일 (1000→1150, +15%) + 깃발 5일 (1140-1170 횡보, 낮은 거래량) +
    돌파 1일 (close 1200 > flag high 1170)."""
    bars = []
    # Pole: +15% rise over 5 days, high volume
    for i, c in enumerate([1000, 1030, 1070, 1110, 1150], start=1):
        bars.append(_bar(c, high=c + 5, low=c - 5, volume=500, day=i))
    # Flag: tight range ~ 2.6% (high 1170, low 1140), low volume
    for i, c in enumerate([1160, 1145, 1155, 1150, 1165], start=6):
        bars.append(_bar(c, high=c + 5, low=c - 5, volume=200, day=i))
    # Breakout
    bars.append(_bar(1200, high=1210, low=1185, volume=500, day=11))
    return bars


def test_detect_clean_flag_breakout() -> None:
    res = detect_flag_breakout(_bullish_flag_bars())
    assert res is not None
    # algorithm tries multiple flag_days; chooses best fit. pole_pct ≥ 10 OK.
    assert res.pole_change_pct >= 10
    assert res.flag_high <= 1175
    assert res.breakout_price == 1200


def test_no_pattern_when_pole_weak() -> None:
    """깃대 +5% 만 → pole_min_pct 10% 미달."""
    bars = []
    for i, c in enumerate([1000, 1010, 1020, 1030, 1050], start=1):
        bars.append(_bar(c, volume=500, day=i))
    for i, c in enumerate([1045, 1050, 1055, 1052, 1048], start=6):
        bars.append(_bar(c, volume=200, day=i))
    bars.append(_bar(1080, volume=500, day=11))
    assert detect_flag_breakout(bars, pole_min_pct=10.0) is None


def test_no_pattern_when_flag_wide() -> None:
    """깃발 폭이 너무 넓음 → 패턴 아님."""
    bars = []
    for i, c in enumerate([1000, 1030, 1070, 1110, 1150], start=1):
        bars.append(_bar(c, volume=500, day=i))
    # Wide flag (~ 12% range)
    for i, c in enumerate([1100, 1240, 1110, 1230, 1120], start=6):
        bars.append(_bar(c, high=c + 15, low=c - 15, volume=200, day=i))
    bars.append(_bar(1260, volume=500, day=11))
    assert detect_flag_breakout(bars, flag_range_max_pct=5.0) is None


def test_no_pattern_when_high_flag_volume() -> None:
    """깃발 동안 거래량이 깃대 보다 높음 → 룰 위반."""
    bars = []
    for i, c in enumerate([1000, 1030, 1070, 1110, 1150], start=1):
        bars.append(_bar(c, volume=500, day=i))
    for i, c in enumerate([1160, 1145, 1155, 1150, 1165], start=6):
        bars.append(_bar(c, volume=900, day=i))  # higher than pole's 500
    bars.append(_bar(1200, volume=500, day=11))
    assert detect_flag_breakout(bars, flag_volume_max_ratio=1.0) is None


def test_no_breakout_when_close_inside_flag() -> None:
    bars = _bullish_flag_bars()
    bars[-1] = _bar(1160, volume=500, day=11)  # close inside flag (<1170 flag high)
    assert detect_flag_breakout(bars) is None


def test_too_few_bars() -> None:
    bars = [_bar(1000 + i * 10, day=i) for i in range(1, 6)]
    assert detect_flag_breakout(bars) is None


def test_invalid_params() -> None:
    bars = _bullish_flag_bars()
    with pytest.raises(ValueError):
        detect_flag_breakout(bars, pole_days=0)
    with pytest.raises(ValueError):
        detect_flag_breakout(bars, flag_days_min=10, flag_days_max=5)


def test_detector_emits_on_breakout() -> None:
    """Detector 가 sliding window 별 다른 flag_high 면 multi-emit 허용
    (different flag boundaries). 적어도 1 emit + 마지막은 day 11 breakout 1200."""
    bus = EventBus()
    sub = bus.subscribe(FlagPennantDetected)
    det = FlagPennantDetector(bus)
    for b in _bullish_flag_bars():
        det.feed(b)
    events = []
    while sub.qsize() > 0:
        events.append(sub.get_nowait())
    assert len(events) >= 1
    # The final emit should reflect the strongest breakout (last bar's close 1200).
    last_emit = events[-1]
    assert last_emit.breakout_price == 1200
    assert last_emit.pole_change_pct >= 10


def test_detector_hysteresis_no_dup() -> None:
    """돌파 후 위쪽 봉 추가 → 같은 flag_high 면 re-emit X.
    Multiple distinct flag_high 들이 sliding window 안 다른 시점에 valid 면
    별개 emit. extra bars above breakout (no new flag) should yield no new emit."""
    bus = EventBus()
    sub = bus.subscribe(FlagPennantDetected)
    det = FlagPennantDetector(bus)
    bars = _bullish_flag_bars()
    for b in bars:
        det.feed(b)
    initial_emit_count = det.emit_count
    # extra bars above breakout — pole_pct check 변하지만 flag_high 같음
    for d, c in zip([12, 13, 14], [1220, 1240, 1260], strict=True):
        det.feed(_bar(c, volume=300, day=d))
    final_emit_count = det.emit_count
    # No additional emits with same flag_high
    assert final_emit_count == initial_emit_count
