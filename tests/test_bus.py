import asyncio
from datetime import UTC, datetime

from ks_ws.bus import EventBus
from ks_ws.events import Event, GapUp, VolumeSpike


def _now():
    return datetime.now(UTC)


def _spike(multiplier=2.0):
    return VolumeSpike(
        symbol="005930",
        timestamp=_now(),
        multiplier=multiplier,
        window_seconds=60,
    )


def _gap(pct=4.0):
    return GapUp(symbol="005930", timestamp=_now(), gap_pct=pct)


def test_publish_delivers_to_matching_subscriber():
    bus = EventBus()
    sub = bus.subscribe(VolumeSpike)
    e = _spike()
    bus.publish(e)
    assert sub.qsize() == 1
    assert sub.get_nowait() == e


def test_publish_skips_non_matching_subscriber():
    bus = EventBus()
    sub = bus.subscribe(GapUp)
    bus.publish(_spike())
    assert sub.qsize() == 0


def test_isinstance_dispatch_to_base_class_subscriber():
    bus = EventBus()
    base_sub = bus.subscribe(Event)
    specific_sub = bus.subscribe(VolumeSpike)
    bus.publish(_spike())
    assert base_sub.qsize() == 1
    assert specific_sub.qsize() == 1


def test_multiple_subscribers_each_receive():
    bus = EventBus()
    s1 = bus.subscribe(VolumeSpike)
    s2 = bus.subscribe(VolumeSpike)
    e = _spike()
    bus.publish(e)
    assert s1.qsize() == 1
    assert s2.qsize() == 1
    assert s1.get_nowait() == e
    assert s2.get_nowait() == e


def test_drop_oldest_on_overflow():
    bus = EventBus()
    sub = bus.subscribe(VolumeSpike, maxsize=2)
    e1 = _spike(multiplier=1.0)
    e2 = _spike(multiplier=2.0)
    e3 = _spike(multiplier=3.0)

    assert sub.offer(e1) is True
    assert sub.offer(e2) is True
    assert sub.offer(e3) is False  # third forces drop of oldest

    assert sub.qsize() == 2
    assert sub.dropped == 1
    assert sub.get_nowait() == e2
    assert sub.get_nowait() == e3


def test_unsubscribe_stops_delivery():
    bus = EventBus()
    sub = bus.subscribe(VolumeSpike)
    bus.publish(_spike())
    bus.unsubscribe(sub)
    bus.publish(_spike())
    assert sub.qsize() == 1


def test_subscription_count_tracks_subscribe_unsubscribe():
    bus = EventBus()
    assert bus.subscription_count == 0
    s = bus.subscribe(Event)
    assert bus.subscription_count == 1
    bus.unsubscribe(s)
    assert bus.subscription_count == 0


def test_async_get_returns_published_event():
    async def run():
        bus = EventBus()
        sub = bus.subscribe(VolumeSpike)
        e = _spike(multiplier=3.5)
        bus.publish(e)
        return await sub.get()

    received = asyncio.run(run())
    assert received.multiplier == 3.5


def test_unrelated_event_types_are_isolated():
    bus = EventBus()
    spike_sub = bus.subscribe(VolumeSpike)
    gap_sub = bus.subscribe(GapUp)

    bus.publish(_spike())
    bus.publish(_gap())

    assert spike_sub.qsize() == 1
    assert gap_sub.qsize() == 1
    assert isinstance(spike_sub.get_nowait(), VolumeSpike)
    assert isinstance(gap_sub.get_nowait(), GapUp)


def test_async_for_iteration():
    async def run():
        bus = EventBus()
        sub = bus.subscribe(VolumeSpike)
        bus.publish(_spike(multiplier=1.0))
        bus.publish(_spike(multiplier=2.0))
        bus.publish(_spike(multiplier=3.0))

        received = []
        async for event in sub:
            received.append(event)
            if len(received) == 3:
                break
        return received

    received = asyncio.run(run())
    assert [e.multiplier for e in received] == [1.0, 2.0, 3.0]
