import asyncio
from datetime import UTC, datetime

from ks_ws.bus import EventBus
from ks_ws.domain import Bar, OrderBook, OrderBookLevel, Tick
from ks_ws.market.hub import MockMarketDataHub, Tier


def _now():
    return datetime.now(UTC)


def _bar(symbol="005930"):
    return Bar(
        symbol=symbol,
        timestamp=_now(),
        timeframe="1m",
        open=70_000,
        high=70_100,
        low=69_900,
        close=70_050,
        volume=1_000,
        value=70_050_000,
    )


def _tick(symbol="005930"):
    return Tick(symbol=symbol, timestamp=_now(), price=70_000, volume=10)


def _orderbook(symbol="005930"):
    return OrderBook(
        symbol=symbol,
        timestamp=_now(),
        bids=(OrderBookLevel(price=69_900, volume=100),),
        asks=(OrderBookLevel(price=70_100, volume=200),),
    )


def test_assign_and_lookup_tier():
    hub = MockMarketDataHub(EventBus())
    hub.assign("005930", Tier.HOT)
    assert hub.tier_of("005930") == Tier.HOT
    assert hub.tier_of("000660") is None


def test_assign_many_and_query_by_tier():
    hub = MockMarketDataHub(EventBus())
    hub.assign_many(
        [
            ("005930", Tier.HOT),
            ("000660", Tier.HOT),
            ("035420", Tier.WARM),
            ("066570", Tier.COLD),
        ]
    )
    assert set(hub.symbols_by_tier(Tier.HOT)) == {"005930", "000660"}
    assert hub.symbols_by_tier(Tier.WARM) == ["035420"]
    assert hub.symbols_by_tier(Tier.COLD) == ["066570"]


def test_remove_drops_assignment():
    hub = MockMarketDataHub(EventBus())
    hub.assign("005930", Tier.HOT)
    hub.remove("005930")
    assert hub.tier_of("005930") is None


def test_remove_nonexistent_is_noop():
    hub = MockMarketDataHub(EventBus())
    hub.remove("005930")  # should not raise


def test_reassigning_overwrites_tier():
    hub = MockMarketDataHub(EventBus())
    hub.assign("005930", Tier.HOT)
    hub.assign("005930", Tier.WARM)
    assert hub.tier_of("005930") == Tier.WARM


def test_feed_bar_publishes_to_bus():
    bus = EventBus()
    sub = bus.subscribe(Bar)
    hub = MockMarketDataHub(bus)
    bar = _bar()
    hub.feed_bar(bar)
    assert sub.qsize() == 1
    assert sub.get_nowait() == bar


def test_feed_tick_publishes_to_bus():
    bus = EventBus()
    sub = bus.subscribe(Tick)
    hub = MockMarketDataHub(bus)
    tick = _tick()
    hub.feed_tick(tick)
    assert sub.get_nowait() == tick


def test_feed_orderbook_publishes_to_bus():
    bus = EventBus()
    sub = bus.subscribe(OrderBook)
    hub = MockMarketDataHub(bus)
    ob = _orderbook()
    hub.feed_orderbook(ob)
    assert sub.get_nowait() == ob


def test_start_stop_lifecycle():
    async def run():
        hub = MockMarketDataHub(EventBus())
        assert hub.started is False
        await hub.start()
        assert hub.started is True
        await hub.stop()
        assert hub.started is False

    asyncio.run(run())


def test_assignments_returns_a_copy():
    hub = MockMarketDataHub(EventBus())
    hub.assign("005930", Tier.HOT)
    snapshot = hub.assignments
    snapshot["XXX"] = Tier.COLD
    assert "XXX" not in hub.assignments
