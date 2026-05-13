"""RealtimeInvestorFlow tests."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ks_ws.bus import EventBus
from ks_ws.sources.realtime_investor_flow import (
    MarketInvestorFlow,
    RealtimeInvestorFlowSource,
    _FlowSnapshot,
    score_from_market_flow,
)

# --- score_from_market_flow ---


def _mk(f: int, i: int, p: int = 0) -> MarketInvestorFlow:
    return MarketInvestorFlow(
        symbol="MARKET", timestamp=datetime.now(UTC), market="KOSPI",
        foreign_net_krw=f, institution_net_krw=i, individual_net_krw=p,
    )


def test_score_strong_buy() -> None:
    """외인 +0.7조 + 기관 +0.5조 = 1.2조 ≥ 1조 → 1.3."""
    assert score_from_market_flow(_mk(700_000_000_000, 500_000_000_000)) == 1.3


def test_score_neutral() -> None:
    assert score_from_market_flow(_mk(0, 0)) == 1.0


def test_score_strong_sell() -> None:
    assert score_from_market_flow(_mk(-1_500_000_000_000, 0)) == 0.7


def test_score_interpolation() -> None:
    # +0.5조 → 1.0 + 0.3 * 0.5 = 1.15
    assert score_from_market_flow(_mk(500_000_000_000, 0)) == pytest.approx(1.15)
    # -0.5조 → 0.85
    assert score_from_market_flow(_mk(-500_000_000_000, 0)) == pytest.approx(0.85)


def test_score_invalid_threshold() -> None:
    with pytest.raises(ValueError):
        score_from_market_flow(_mk(0, 0), strong_krw=0)


# --- RealtimeInvestorFlowSource ---


def test_source_invalid_market() -> None:
    bus = EventBus()
    with pytest.raises(ValueError):
        RealtimeInvestorFlowSource(bus, markets=("BAD",))


def test_source_invalid_interval() -> None:
    bus = EventBus()
    with pytest.raises(ValueError):
        RealtimeInvestorFlowSource(bus, interval_sec=0)


def test_source_step_publishes_event() -> None:
    bus = EventBus()
    sub = bus.subscribe(MarketInvestorFlow)
    mock_snap = _FlowSnapshot(
        foreign_net_krw=+1_500_000_000_000,  # +1.5조
        institution_net_krw=-500_000_000_000,
        individual_net_krw=-1_000_000_000_000,
    )
    source = RealtimeInvestorFlowSource(
        bus, markets=("KOSPI",), fetcher=lambda m: mock_snap,
    )
    polled = source.step()
    assert polled == 1
    events = []
    while sub.qsize() > 0:
        events.append(sub.get_nowait())
    assert len(events) == 1
    ev = events[0]
    assert ev.market == "KOSPI"
    assert ev.foreign_net_krw == 1_500_000_000_000
    assert ev.institution_net_krw == -500_000_000_000


def test_source_step_skips_none_fetch() -> None:
    """Fetcher returns None (time limit / error) → no publish."""
    bus = EventBus()
    sub = bus.subscribe(MarketInvestorFlow)
    source = RealtimeInvestorFlowSource(
        bus, markets=("KOSPI",), fetcher=lambda m: None,
    )
    polled = source.step()
    assert polled == 0
    assert sub.qsize() == 0


def test_source_multiple_markets() -> None:
    bus = EventBus()
    sub = bus.subscribe(MarketInvestorFlow)
    source = RealtimeInvestorFlowSource(
        bus, markets=("KOSPI", "KOSDAQ"),
        fetcher=lambda m: _FlowSnapshot(100, 200, 0),
    )
    polled = source.step()
    assert polled == 2
    markets_seen = set()
    while sub.qsize() > 0:
        markets_seen.add(sub.get_nowait().market)
    assert markets_seen == {"KOSPI", "KOSDAQ"}


def test_source_fetch_exception_isolated() -> None:
    """One market's fetcher exception doesn't kill the other."""
    bus = EventBus()
    sub = bus.subscribe(MarketInvestorFlow)

    def flaky(m: str):
        if m == "KOSPI":
            raise RuntimeError("boom")
        return _FlowSnapshot(0, 0, 0)

    source = RealtimeInvestorFlowSource(
        bus, markets=("KOSPI", "KOSDAQ"), fetcher=flaky,
    )
    polled = source.step()
    assert polled == 1
    assert sub.qsize() == 1


def test_source_start_stop() -> None:
    bus = EventBus()
    source = RealtimeInvestorFlowSource(
        bus, markets=("KOSPI",), interval_sec=0.05,
        fetcher=lambda m: _FlowSnapshot(0, 0, 0),
    )

    async def run() -> None:
        await source.start()
        await asyncio.sleep(0.12)  # let 1-2 polls happen
        await source.stop()

    asyncio.run(run())
    assert source.poll_count >= 1
    assert not source.running
