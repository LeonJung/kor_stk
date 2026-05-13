"""DynamicMacroUpdater — MarketInvestorFlow event → set_macro_score 매 분 갱신.

fundamental_strategy.md §3 Pattern 7 (Regime-Based Strategy Activation) 의 라이브
구현. 시작 시 set_macro_score 로 정해진 base score 는 그 종목 자체 fundamental
(RVOL + valuation + 어제 외인 일별 누적). 시장 전체 외인+기관 흐름이 실시간
바뀌면 종목별 score 를 비례 조정 → BUY entry / sizing 이 시장 regime 따라 즉시
달라짐.

흐름:
- 시작 시 paper_trade 가 각 종목 base score = blend(RVOL, foreign_daily, valuation) 을
  set_macro_score 로 allocator 에 저장. DynamicMacroUpdater 가 이 값을 own snapshot
  으로 capture (`set_base_score`).
- RealtimeInvestorFlowSource 가 60s 마다 MarketInvestorFlow (KOSPI/KOSDAQ) emit.
- updater 가 MarketInvestorFlow event 받아 score_from_market_flow → [0.7, 1.3] 의
  regime_multiplier 산출. 해당 market 의 모든 종목 → new = base * regime,
  [0.0, 1.5] clamp, allocator.set_macro_score 재설정.

종목 → 시장 매핑: 사용자가 set_symbol_market("005930", "KOSPI") 로 등록.
미등록 종목은 regime 영향 X (base 유지).
"""

from __future__ import annotations

import logging
from typing import Protocol

from ks_ws.bus import EventBus
from ks_ws.sources.realtime_investor_flow import (
    MarketInvestorFlow,
    score_from_market_flow,
)

log = logging.getLogger("ks_ws.sources.dynamic_macro")


class _AllocatorLike(Protocol):
    def set_macro_score(self, symbol: str, score: float) -> None: ...


def _clamp(value: float, lo: float = 0.0, hi: float = 1.5) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


class DynamicMacroUpdater:
    """Subscribe to MarketInvestorFlow → recompute per-symbol macro_score live.

    - ``allocator`` — FundamentalAllocator (duck-typed via set_macro_score).
    - ``base_scores`` — initial per-symbol score (RVOL+valuation+daily foreign).
    - ``symbol_markets`` — symbol → market name ("KOSPI"/"KOSDAQ").
    - ``score_fn`` — MarketInvestorFlow → regime multiplier in [0.7, 1.3]
      (default: score_from_market_flow with strong=1조).
    """

    def __init__(
        self,
        bus: EventBus,
        allocator: _AllocatorLike,
        *,
        base_scores: dict[str, float] | None = None,
        symbol_markets: dict[str, str] | None = None,
        strong_krw: int = 1_000_000_000_000,
    ) -> None:
        if strong_krw <= 0:
            raise ValueError("strong_krw must be positive")
        self._bus = bus
        self._allocator = allocator
        self._base: dict[str, float] = dict(base_scores or {})
        self._sym_market: dict[str, str] = dict(symbol_markets or {})
        self._strong_krw = strong_krw
        self._latest_regime: dict[str, float] = {}  # market → multiplier
        self.update_count = 0
        self._sub = None
        self._task = None

    def set_base_score(self, symbol: str, score: float) -> None:
        if score < 0:
            raise ValueError("score must be non-negative")
        self._base[symbol] = score

    def set_symbol_market(self, symbol: str, market: str) -> None:
        if market not in ("KOSPI", "KOSDAQ"):
            raise ValueError(f"unknown market: {market!r}")
        self._sym_market[symbol] = market

    def base_score(self, symbol: str) -> float | None:
        return self._base.get(symbol)

    def latest_regime(self, market: str) -> float | None:
        return self._latest_regime.get(market)

    def apply_flow(self, flow: MarketInvestorFlow) -> int:
        """Synchronous core — called by event subscriber or test. Returns
        number of symbols whose score was updated."""
        regime = score_from_market_flow(flow, strong_krw=self._strong_krw)
        self._latest_regime[flow.market] = regime
        n = 0
        for sym, mk in self._sym_market.items():
            if mk != flow.market:
                continue
            base = self._base.get(sym)
            if base is None:
                continue
            new = _clamp(base * regime)
            self._allocator.set_macro_score(sym, new)
            n += 1
        self.update_count += 1
        log.info("dynamic macro: market=%s regime=%.2f updated %d symbols",
                 flow.market, regime, n)
        return n

    async def start(self) -> None:
        """Async subscribe loop. Must be run inside an asyncio loop."""
        import asyncio

        if self._task is not None:
            return
        self._sub = self._bus.subscribe(MarketInvestorFlow, maxsize=1_000)

        async def _loop() -> None:
            try:
                async for flow in self._sub:
                    self.apply_flow(flow)
            except asyncio.CancelledError:
                pass

        self._task = asyncio.create_task(_loop())

    async def stop(self) -> None:
        import asyncio
        import contextlib

        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._sub is not None:
            self._sub.close()
            self._sub = None
