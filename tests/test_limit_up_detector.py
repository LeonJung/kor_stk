"""Tests for LimitUpDetector."""

from datetime import UTC, datetime

from ks_ws.detectors.limit_up import LimitUpDetector
from ks_ws.domain import OrderBook, OrderBookLevel, Tick
from ks_ws.events import LimitUpBroken, LimitUpReached


def _ts(seconds: int = 0):
    return datetime(2026, 5, 11, 9, 30, seconds, tzinfo=UTC)


def test_emits_limit_up_reached_when_price_hits():
    events = []
    det = LimitUpDetector(
        symbols={"A005930": (10000, 13000)}, emit=events.append
    )
    det.feed_tick(Tick(symbol="A005930", timestamp=_ts(), price=12950, volume=10))
    assert events == []
    det.feed_tick(Tick(symbol="A005930", timestamp=_ts(1), price=13000, volume=10))
    assert len(events) == 1
    assert isinstance(events[0], LimitUpReached)
    assert events[0].limit_up_price == 13000


def test_emits_only_once_per_reach():
    events = []
    det = LimitUpDetector(
        symbols={"A005930": (10000, 13000)}, emit=events.append
    )
    det.feed_tick(Tick(symbol="A005930", timestamp=_ts(), price=13000, volume=10))
    det.feed_tick(Tick(symbol="A005930", timestamp=_ts(1), price=13000, volume=10))
    det.feed_tick(Tick(symbol="A005930", timestamp=_ts(2), price=13000, volume=10))
    assert len(events) == 1


def test_emits_limit_up_broken_when_price_drops():
    events = []
    det = LimitUpDetector(
        symbols={"A005930": (10000, 13000)}, emit=events.append
    )
    det.feed_tick(Tick(symbol="A005930", timestamp=_ts(), price=13000, volume=10))
    det.feed_tick(Tick(symbol="A005930", timestamp=_ts(5), price=12990, volume=10))
    assert len(events) == 2
    assert isinstance(events[0], LimitUpReached)
    assert isinstance(events[1], LimitUpBroken)
    assert events[1].current_price == 12990


def test_can_re_reach_after_break():
    events = []
    det = LimitUpDetector(
        symbols={"A005930": (10000, 13000)}, emit=events.append
    )
    det.feed_tick(Tick(symbol="A005930", timestamp=_ts(), price=13000, volume=10))
    det.feed_tick(Tick(symbol="A005930", timestamp=_ts(5), price=12500, volume=10))
    det.feed_tick(Tick(symbol="A005930", timestamp=_ts(10), price=13000, volume=10))
    types = [type(e).__name__ for e in events]
    assert types == ["LimitUpReached", "LimitUpBroken", "LimitUpReached"]


def test_orderbook_best_bid_drop_detects_break():
    events = []
    det = LimitUpDetector(
        symbols={"A005930": (10000, 13000)}, emit=events.append
    )
    det.feed_tick(Tick(symbol="A005930", timestamp=_ts(), price=13000, volume=10))
    # best_bid = 12950 < 13000 → broken
    ob = OrderBook(
        symbol="A005930",
        timestamp=_ts(2),
        bids=(OrderBookLevel(price=12950, volume=100),),
        asks=(OrderBookLevel(price=13000, volume=100),),
    )
    det.feed_orderbook(ob)
    assert any(isinstance(e, LimitUpBroken) for e in events)


def test_orderbook_no_effect_when_not_yet_reached():
    events = []
    det = LimitUpDetector(
        symbols={"A005930": (10000, 13000)}, emit=events.append
    )
    ob = OrderBook(
        symbol="A005930",
        timestamp=_ts(),
        bids=(OrderBookLevel(price=12000, volume=100),),
        asks=(OrderBookLevel(price=12100, volume=100),),
    )
    det.feed_orderbook(ob)
    assert events == []


def test_untracked_symbol_no_emit():
    events = []
    det = LimitUpDetector(
        symbols={"A005930": (10000, 13000)}, emit=events.append
    )
    det.feed_tick(Tick(symbol="A000660", timestamp=_ts(), price=99999, volume=10))
    assert events == []


def test_state_inspection():
    events = []
    det = LimitUpDetector(
        symbols={"A005930": (10000, 13000)}, emit=events.append
    )
    assert det.state_of("A005930") == "not_reached"
    assert det.state_of("A000660") == "untracked"
    det.feed_tick(Tick(symbol="A005930", timestamp=_ts(), price=13000, volume=10))
    assert det.state_of("A005930") == "reached"
    det.feed_tick(Tick(symbol="A005930", timestamp=_ts(1), price=12500, volume=10))
    assert det.state_of("A005930") == "not_reached"
