"""Tests for ForeignNetBuySource."""

import asyncio

import pytest

from ks_ws.bus import EventBus
from ks_ws.events import ForeignNetBuy
from ks_ws.sources.foreign_flow import ForeignNetBuySource


def test_step_publishes_event_per_symbol():
    bus = EventBus()
    sub = bus.subscribe(ForeignNetBuy)
    src = ForeignNetBuySource(
        bus, ["A", "B", "C"],
        fetcher=lambda s: {"A": 100, "B": -50, "C": 0}[s],
    )
    polled = src.step()
    assert polled == 3
    events = []
    while sub.qsize() > 0:
        events.append(sub.get_nowait())
    assert {e.symbol: e.delta_krw for e in events} == {"A": 100, "B": -50, "C": 0}


def test_failed_fetcher_logged_not_raised():
    bus = EventBus()
    sub = bus.subscribe(ForeignNetBuy)
    calls = []

    def fetcher(s):
        calls.append(s)
        if s == "BAD":
            raise RuntimeError("boom")
        return 42

    src = ForeignNetBuySource(bus, ["GOOD", "BAD", "GOOD2"], fetcher=fetcher)
    src.step()
    assert calls == ["GOOD", "BAD", "GOOD2"]
    events = []
    while sub.qsize() > 0:
        events.append(sub.get_nowait())
    syms = [e.symbol for e in events]
    assert "GOOD" in syms
    assert "GOOD2" in syms
    assert "BAD" not in syms


def test_validation():
    bus = EventBus()
    with pytest.raises(ValueError, match="interval_sec"):
        ForeignNetBuySource(bus, ["A"], fetcher=lambda s: 0, interval_sec=0)


def test_poll_count_increments():
    bus = EventBus()
    src = ForeignNetBuySource(bus, ["A"], fetcher=lambda s: 0)
    src.step()
    src.step()
    assert src.poll_count == 2


def test_async_start_stop_lifecycle():
    async def run():
        bus = EventBus()
        src = ForeignNetBuySource(bus, ["A"], fetcher=lambda s: 0, interval_sec=0.05)
        await src.start()
        assert src.running
        await asyncio.sleep(0.12)
        await src.stop()
        assert not src.running
        assert src.poll_count >= 1

    asyncio.run(run())
