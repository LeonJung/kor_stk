"""BaseMacroRefresher — 시작 시 set 한 base_macro 를 장중 주기적으로 재계산.

기존 흐름 (cycle 10 task 2 / cycle 11 task 1-2):
- paper_trade 시작 시점에 RVOL + 외인 5일 trend + valuation + multi-timeframe
  regime blend → base_macro[symbol] 한 번 set.
- DynamicMacroUpdater 가 RealtimeInvestorFlow event (60s) 받아 base_macro *
  regime → allocator.set_macro_score 매 분 갱신.

미흡:
- 분봉 trend (mtr.minute_score) 는 시작 시점 데이터로만 산출 — 장중 변함 X.
- 외인 5일 trend 는 어제까지 데이터만 — 장중에는 변할 일 없어 OK.
- RVOL 도 일봉 끝나야 갱신.

본 모듈 = **분봉 momentum (mtr.minute_score)** 을 장중 매 N분 재계산해서
base_macro 갱신. DynamicMacroUpdater 가 그걸 base 로 보고 regime 곱.

설계:
- ``BaseMacroRefresher(bus, bar_store, allocator, dyn_macro, codes, kospi_bars,
  foreign_scores, valuation_scores, rvol_scores)`` —
  - 시작 시 받은 정적 score (foreign/valuation/rvol) 는 그대로 유지.
  - 매 ``interval_sec`` 마다 각 종목 BarStore("1m") 분봉 최근 30개 → mtr.minute_score
    재계산 → blend_macro_scores(rvol, foreign, valuation, new_mtr) → set_macro_score
    + dyn_macro.set_base_score 둘 다 갱신.
- DynamicMacroUpdater 가 이후 MarketInvestorFlow event 받으면 *새* base * regime
  으로 곱.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from typing import Protocol

from ks_ws.bus import EventBus
from ks_ws.domain import Bar
from ks_ws.sources.macro_score import blend_macro_scores
from ks_ws.sources.multi_timeframe_regime import compute_multi_regime
from ks_ws.storage.bars import BarStore

log = logging.getLogger("ks_ws.sources.base_macro_refresher")


class _AllocatorLike(Protocol):
    def set_macro_score(self, symbol: str, score: float) -> None: ...


class _DynMacroLike(Protocol):
    def set_base_score(self, symbol: str, score: float) -> None: ...


class BaseMacroRefresher:
    """장중 분봉 mtr.minute_score 재계산 → base_macro 재 set."""

    def __init__(
        self,
        bus: EventBus,
        bar_store: BarStore,
        allocator: _AllocatorLike,
        dyn_macro: _DynMacroLike,
        *,
        codes: Sequence[str],
        kospi_bars: Sequence[Bar],
        static_scores: dict[str, tuple[float, float, float]],
        interval_sec: float = 300.0,  # 5분 — 분봉 momentum 갱신 주기
        minute_lookback: int = 30,
    ) -> None:
        if interval_sec <= 0:
            raise ValueError("interval_sec must be positive")
        if minute_lookback <= 0:
            raise ValueError("minute_lookback must be positive")
        self._bus = bus
        self._bar_store = bar_store
        self._allocator = allocator
        self._dyn_macro = dyn_macro
        self._codes = list(codes)
        self._kospi_bars = list(kospi_bars)
        self._static = dict(static_scores)
        self.interval_sec = interval_sec
        self.minute_lookback = minute_lookback
        self._task: asyncio.Task[None] | None = None
        self.refresh_count = 0
        self.last_scores: dict[str, float] = {}

    def step(self) -> int:
        """Synchronous core. Recompute mtr.minute_score per symbol and re-set
        base_macro. Returns number of symbols updated."""
        updated = 0
        for sym in self._codes:
            static = self._static.get(sym)
            if static is None:
                continue
            r_score, f_score, v_score = static
            minute_bars = list(self._bar_store.read(sym, "1m"))
            mtr = compute_multi_regime(
                index_bars=self._kospi_bars,
                minute_bars=minute_bars[-self.minute_lookback:] if minute_bars else [],
            )
            new_base = blend_macro_scores(r_score, f_score, v_score, mtr.combined)
            self._allocator.set_macro_score(sym, new_base)
            self._dyn_macro.set_base_score(sym, new_base)
            self.last_scores[sym] = new_base
            updated += 1
        self.refresh_count += 1
        log.info(
            "base macro refreshed: %d/%d symbols (interval=%ss)",
            updated, len(self._codes), self.interval_sec,
        )
        return updated

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.interval_sec)
                await asyncio.to_thread(self.step)
        except asyncio.CancelledError:
            pass
