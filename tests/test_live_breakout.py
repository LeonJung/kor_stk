"""LiveBreakoutStrategy — edge detection + same-day single entry guards.

5/12 paper trade 에서 발견된 사팔사팔 (009150 13B/6S) 문제 fix 검증.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ks_ws.domain import Side, Tick
from ks_ws.strategies.live_breakout import LiveBreakoutStrategy


def _t(price: int, *, ts: datetime, volume: int = 100, symbol: str = "005930") -> Tick:
    return Tick(symbol=symbol, price=price, volume=volume, timestamp=ts)


def _strategy(high: int = 1000) -> LiveBreakoutStrategy:
    return LiveBreakoutStrategy(
        high60={"005930": high}, take_profit_pct=2.0, stop_loss_pct=3.0,
        max_hold_minutes=60, confidence=0.7,
    )


def _warmup_ticks(s: LiveBreakoutStrategy, base_ts: datetime, *, below: int) -> None:
    """Feed 20 below-high ticks so volume window is populated."""
    for i in range(20):
        s.on_tick(_t(below, ts=base_ts + timedelta(seconds=i), volume=50))


def test_edge_detection_no_signal_when_already_above() -> None:
    """가격이 처음부터 high60 위에서 출발하면 cross 가 없으므로 signal 안 남."""
    s = _strategy(high=1000)
    start = datetime(2026, 5, 12, 9, 0, tzinfo=UTC)
    # First tick already above — was_above default False, so this looks like cross,
    # but volume gate blocks because window has 1 tick.
    sigs = s.on_tick(_t(1100, ts=start, volume=500))
    assert sigs == []
    # Subsequent ticks still above — was_above True now, no edge → no signal
    for i in range(1, 25):
        sigs = s.on_tick(_t(1100, ts=start + timedelta(seconds=i), volume=500))
        assert sigs == [], f"unexpected signal at tick {i}"


def test_edge_detection_triggers_on_cross() -> None:
    """가격이 high60 아래에서 위로 cross 하는 순간만 BUY signal."""
    s = _strategy(high=1000)
    base = datetime(2026, 5, 12, 9, 0, tzinfo=UTC)
    _warmup_ticks(s, base, below=950)  # 20 ticks below

    # Tick 21: cross above with volume spike (>= 1.5x avg = 75)
    cross = _t(1010, ts=base + timedelta(seconds=21), volume=200)
    sigs = s.on_tick(cross)
    assert len(sigs) == 1
    assert sigs[0].side is Side.BUY
    assert sigs[0].strategy == "breakout"


def test_no_re_signal_while_holding_above() -> None:
    """Cross 후 가격이 high 위에서 머무는 동안 추가 BUY signal X (사팔사팔 방지)."""
    s = _strategy(high=1000)
    base = datetime(2026, 5, 12, 9, 0, tzinfo=UTC)
    _warmup_ticks(s, base, below=950)
    s.on_tick(_t(1010, ts=base + timedelta(seconds=21), volume=200))  # entry

    # TP 도달로 청산 → 같은 날 재진입 차단되어야 함
    tp_price = 1031  # 1010 * 1.02 = 1030.2 → 1031 to clear
    sell = s.on_tick(_t(tp_price, ts=base + timedelta(seconds=22), volume=100))
    assert sell and sell[0].side is Side.SELL

    # 청산 후 가격이 다시 high60 위 cross — 같은 날이라 entry guard 가 차단
    # First push below high to set was_above=False
    s.on_tick(_t(990, ts=base + timedelta(seconds=23), volume=100))
    # Now cross above again with volume spike
    re_cross = _t(1015, ts=base + timedelta(seconds=24), volume=200)
    sigs = s.on_tick(re_cross)
    assert sigs == [], "same-day re-entry must be blocked"


def test_next_day_re_entry_allowed() -> None:
    """다음 거래일에는 같은 종목 entry 다시 가능."""
    s = _strategy(high=1000)
    day1 = datetime(2026, 5, 12, 9, 0, tzinfo=UTC)
    _warmup_ticks(s, day1, below=950)
    sigs = s.on_tick(_t(1010, ts=day1 + timedelta(seconds=21), volume=200))
    assert sigs and sigs[0].side is Side.BUY
    # Force-close the position (SL hit) so _open is cleared.
    sl_price = 979  # 1010 * 0.97 = 979.7 → 979 to clear
    s.on_tick(_t(sl_price, ts=day1 + timedelta(seconds=22), volume=100))

    # Day 2: same symbol, cross above high60 again, volume warmup re-seeded
    day2 = datetime(2026, 5, 13, 9, 0, tzinfo=UTC)
    _warmup_ticks(s, day2, below=950)
    re_cross = _t(1010, ts=day2 + timedelta(seconds=21), volume=200)
    sigs = s.on_tick(re_cross)
    assert len(sigs) == 1 and sigs[0].side is Side.BUY


def test_below_high_no_signal() -> None:
    """가격이 high60 아래에 있는 동안은 모든 tick 무신호."""
    s = _strategy(high=1000)
    base = datetime(2026, 5, 12, 9, 0, tzinfo=UTC)
    for i in range(30):
        sigs = s.on_tick(_t(950, ts=base + timedelta(seconds=i), volume=200))
        assert sigs == []


def test_tp_sl_timeout_still_work() -> None:
    """청산 룰 (TP / SL / hold timeout) 은 그대로 동작."""
    s = _strategy(high=1000)
    base = datetime(2026, 5, 12, 9, 0, tzinfo=UTC)
    _warmup_ticks(s, base, below=950)
    s.on_tick(_t(1010, ts=base + timedelta(seconds=21), volume=200))  # entry @1010
    # Hold timeout: max_hold_minutes=60 → 60min+ 후 SELL
    tout = s.on_tick(_t(1015, ts=base + timedelta(seconds=21, minutes=61), volume=100))
    assert tout and tout[0].side is Side.SELL
    assert "timeout" in (tout[0].note or "")
