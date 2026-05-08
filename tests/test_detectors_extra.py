from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.bus import EventBus
from ks_ws.detectors.gap_up import GapUpDetector
from ks_ws.detectors.orderbook_imbalance import OrderbookImbalanceDetector
from ks_ws.detectors.volume_spike import VolumeSpikeDetector
from ks_ws.domain import Bar, OrderBook, OrderBookLevel
from ks_ws.events import GapUp, OrderbookImbalance, VolumeSpike


def _bar(symbol="005930", *, volume=1000, close=70_000, open_=None, ts_offset=0):
    return Bar(
        symbol=symbol,
        timestamp=datetime(2026, 5, 8, tzinfo=UTC) + timedelta(minutes=ts_offset),
        timeframe="1m",
        open=open_ if open_ is not None else close,
        high=close,
        low=close,
        close=close,
        volume=volume,
        value=close * volume,
    )


def _ob(symbol="005930", *, bid_vols=None, ask_vols=None):
    bid_vols = bid_vols or [100] * 5
    ask_vols = ask_vols or [100] * 5
    return OrderBook(
        symbol=symbol,
        timestamp=datetime.now(UTC),
        bids=tuple(OrderBookLevel(price=70_000 - i * 10, volume=v) for i, v in enumerate(bid_vols)),
        asks=tuple(OrderBookLevel(price=70_010 + i * 10, volume=v) for i, v in enumerate(ask_vols)),
    )


# VolumeSpikeDetector ------------------------------------------------------


def test_volume_spike_emits_when_ratio_exceeds_multiplier():
    bus = EventBus()
    sub = bus.subscribe(VolumeSpike)
    d = VolumeSpikeDetector(bus, window=3, multiplier=3.0, cooldown_multiplier=1.5)
    # Prime baseline with three 1000-volume bars
    for _ in range(3):
        d.feed(_bar(volume=1000))
    # Spike: 4000 (4x baseline of 1000) — should fire
    d.feed(_bar(volume=4000))
    assert sub.qsize() == 1
    e = sub.get_nowait()
    assert e.multiplier >= 3.0


def test_volume_spike_no_fire_below_baseline():
    bus = EventBus()
    sub = bus.subscribe(VolumeSpike)
    d = VolumeSpikeDetector(bus, window=3, multiplier=3.0, cooldown_multiplier=1.5)
    for _ in range(3):
        d.feed(_bar(volume=1000))
    d.feed(_bar(volume=2000))  # 2x — below 3x threshold
    assert sub.qsize() == 0


def test_volume_spike_warmup_suppresses_until_window_filled():
    bus = EventBus()
    sub = bus.subscribe(VolumeSpike)
    d = VolumeSpikeDetector(bus, window=5, multiplier=3.0, cooldown_multiplier=1.5)
    # Only 2 bars priming — even a huge volume must not fire before window=5.
    d.feed(_bar(volume=10))
    d.feed(_bar(volume=10))
    d.feed(_bar(volume=100_000))
    assert sub.qsize() == 0


def test_volume_spike_hysteresis_prevents_repeats():
    bus = EventBus()
    sub = bus.subscribe(VolumeSpike)
    d = VolumeSpikeDetector(bus, window=3, multiplier=3.0, cooldown_multiplier=1.5)
    for _ in range(3):
        d.feed(_bar(volume=1000))
    d.feed(_bar(volume=4000))  # spike fires
    d.feed(_bar(volume=4500))  # still high, must not re-fire
    assert sub.qsize() == 1


def test_volume_spike_param_validation():
    bus = EventBus()
    with pytest.raises(ValueError):
        VolumeSpikeDetector(bus, window=1)
    with pytest.raises(ValueError):
        VolumeSpikeDetector(bus, multiplier=1.5, cooldown_multiplier=1.5)


# OrderbookImbalanceDetector -----------------------------------------------


def test_orderbook_imbalance_fires_when_buy_pressure_exceeds_threshold():
    bus = EventBus()
    sub = bus.subscribe(OrderbookImbalance)
    d = OrderbookImbalanceDetector(bus, levels=3, buy_threshold=2.0, cooldown_threshold=1.3)
    # bid sum = 600, ask sum = 200 → ratio 3.0
    d.feed(_ob(bid_vols=[200, 200, 200, 100, 100], ask_vols=[100, 50, 50, 50, 50]))
    assert sub.qsize() == 1
    e = sub.get_nowait()
    assert e.bid_to_ask_ratio == pytest.approx(3.0)
    assert e.levels_used == 3


def test_orderbook_imbalance_below_threshold_does_not_fire():
    bus = EventBus()
    sub = bus.subscribe(OrderbookImbalance)
    d = OrderbookImbalanceDetector(bus, levels=3, buy_threshold=2.0, cooldown_threshold=1.3)
    d.feed(_ob(bid_vols=[150, 150, 150], ask_vols=[100, 100, 100]))  # ratio 1.5
    assert sub.qsize() == 0


def test_orderbook_imbalance_zero_ask_ignored():
    """Empty ask side must not raise — sometimes happens around limit-up."""
    bus = EventBus()
    sub = bus.subscribe(OrderbookImbalance)
    d = OrderbookImbalanceDetector(bus, levels=3)
    d.feed(_ob(bid_vols=[100] * 5, ask_vols=[0, 0, 0, 0, 0]))
    assert sub.qsize() == 0


def test_orderbook_imbalance_param_validation():
    bus = EventBus()
    with pytest.raises(ValueError):
        OrderbookImbalanceDetector(bus, buy_threshold=1.5, cooldown_threshold=1.5)
    with pytest.raises(ValueError):
        OrderbookImbalanceDetector(bus, levels=0)


# GapUpDetector ------------------------------------------------------------


def test_gap_up_fires_when_open_exceeds_prior_close_by_threshold():
    bus = EventBus()
    sub = bus.subscribe(GapUp)
    d = GapUpDetector(bus, gap_pct_threshold=3.0)
    # First bar establishes prior close; cannot fire on the first bar.
    d.feed(_bar(close=70_000, open_=70_000))
    # Next session opens at 73_000 against prior close 70_000 → +4.29%
    d.feed(_bar(close=73_500, open_=73_000))
    assert sub.qsize() == 1
    e = sub.get_nowait()
    assert e.gap_pct == pytest.approx(4.2857, rel=1e-3)


def test_gap_up_does_not_fire_below_threshold():
    bus = EventBus()
    sub = bus.subscribe(GapUp)
    d = GapUpDetector(bus, gap_pct_threshold=3.0)
    d.feed(_bar(close=70_000, open_=70_000))
    d.feed(_bar(close=71_000, open_=71_000))  # +1.43%
    assert sub.qsize() == 0


def test_gap_up_no_fire_on_first_bar():
    bus = EventBus()
    sub = bus.subscribe(GapUp)
    d = GapUpDetector(bus)
    d.feed(_bar(close=70_000, open_=80_000))  # +14.3% but no prior — skip
    assert sub.qsize() == 0


def test_gap_up_per_symbol_state():
    bus = EventBus()
    sub = bus.subscribe(GapUp)
    d = GapUpDetector(bus, gap_pct_threshold=3.0)
    # AAA established with prior close 70_000
    d.feed(_bar(symbol="AAA", close=70_000, open_=70_000))
    # BBB has no prior close yet, can't fire
    d.feed(_bar(symbol="BBB", close=80_000, open_=85_000))
    # AAA next session gaps up 5%
    d.feed(_bar(symbol="AAA", close=73_500, open_=73_500))
    assert sub.qsize() == 1
    e = sub.get_nowait()
    assert e.symbol == "AAA"


def test_gap_up_param_validation():
    bus = EventBus()
    with pytest.raises(ValueError):
        GapUpDetector(bus, gap_pct_threshold=0)
