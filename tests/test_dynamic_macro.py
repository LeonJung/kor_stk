"""DynamicMacroUpdater — MarketInvestorFlow → per-symbol score 갱신."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ks_ws.bus import EventBus
from ks_ws.sources.dynamic_macro import DynamicMacroUpdater
from ks_ws.sources.realtime_investor_flow import MarketInvestorFlow


class _StubAlloc:
    def __init__(self) -> None:
        self.scores: dict[str, float] = {}

    def set_macro_score(self, symbol: str, score: float) -> None:
        self.scores[symbol] = score


def _flow(market: str, foreign: int, institution: int = 0) -> MarketInvestorFlow:
    return MarketInvestorFlow(
        symbol="MARKET", timestamp=datetime.now(UTC),
        market=market, foreign_net_krw=foreign,
        institution_net_krw=institution, individual_net_krw=0,
    )


def test_apply_flow_updates_scores_for_market_symbols() -> None:
    bus = EventBus()
    alloc = _StubAlloc()
    upd = DynamicMacroUpdater(
        bus, alloc,
        base_scores={"005930": 1.0, "000660": 0.8, "035720": 1.2},
        symbol_markets={"005930": "KOSPI", "000660": "KOSPI", "035720": "KOSDAQ"},
    )
    # Strong KOSPI foreign+inst buy → regime 1.3
    n = upd.apply_flow(_flow("KOSPI", foreign=600_000_000_000, institution=600_000_000_000))
    assert n == 2  # only KOSPI symbols
    assert alloc.scores["005930"] == pytest.approx(1.3, rel=0.01)
    assert alloc.scores["000660"] == pytest.approx(1.04, rel=0.01)
    assert "035720" not in alloc.scores  # KOSDAQ unaffected


def test_apply_flow_clamps_to_max_1_5() -> None:
    bus = EventBus()
    alloc = _StubAlloc()
    upd = DynamicMacroUpdater(
        bus, alloc,
        base_scores={"005930": 1.4},
        symbol_markets={"005930": "KOSPI"},
    )
    upd.apply_flow(_flow("KOSPI", foreign=1_000_000_000_000, institution=1_000_000_000_000))
    assert alloc.scores["005930"] == pytest.approx(1.5)  # clamped (1.4 * 1.3 = 1.82 → 1.5)


def test_apply_flow_clamps_negative_to_zero() -> None:
    bus = EventBus()
    alloc = _StubAlloc()
    upd = DynamicMacroUpdater(
        bus, alloc,
        base_scores={"005930": 0.0},
        symbol_markets={"005930": "KOSPI"},
    )
    upd.apply_flow(_flow("KOSPI", foreign=-2_000_000_000_000))
    assert alloc.scores["005930"] == pytest.approx(0.0)


def test_unregistered_symbol_skipped() -> None:
    bus = EventBus()
    alloc = _StubAlloc()
    upd = DynamicMacroUpdater(
        bus, alloc,
        base_scores={"005930": 1.0},  # no symbol_markets entry
    )
    n = upd.apply_flow(_flow("KOSPI", foreign=500_000_000_000))
    assert n == 0
    assert alloc.scores == {}


def test_setters() -> None:
    bus = EventBus()
    alloc = _StubAlloc()
    upd = DynamicMacroUpdater(bus, alloc)
    upd.set_base_score("005930", 1.1)
    upd.set_symbol_market("005930", "KOSPI")
    upd.apply_flow(_flow("KOSPI", foreign=500_000_000_000))
    # regime = 1.0 + 0.3 * 0.5 = 1.15 → 1.1 * 1.15 = 1.265
    assert alloc.scores["005930"] == pytest.approx(1.265, rel=0.01)


def test_invalid_market_raises() -> None:
    bus = EventBus()
    alloc = _StubAlloc()
    upd = DynamicMacroUpdater(bus, alloc)
    with pytest.raises(ValueError):
        upd.set_symbol_market("005930", "NYSE")


def test_invalid_base_score_raises() -> None:
    bus = EventBus()
    alloc = _StubAlloc()
    upd = DynamicMacroUpdater(bus, alloc)
    with pytest.raises(ValueError):
        upd.set_base_score("005930", -0.1)


def test_invalid_strong_krw_raises() -> None:
    bus = EventBus()
    alloc = _StubAlloc()
    with pytest.raises(ValueError):
        DynamicMacroUpdater(bus, alloc, strong_krw=0)


def test_async_subscribe_drives_apply_flow() -> None:
    async def _run() -> None:
        bus = EventBus()
        alloc = _StubAlloc()
        upd = DynamicMacroUpdater(
            bus, alloc,
            base_scores={"005930": 1.0},
            symbol_markets={"005930": "KOSPI"},
        )
        await upd.start()
        try:
            bus.publish(_flow("KOSPI", foreign=500_000_000_000))
            await asyncio.sleep(0.05)
            assert alloc.scores.get("005930") == pytest.approx(1.15, rel=0.01)
        finally:
            await upd.stop()

    asyncio.run(_run())
