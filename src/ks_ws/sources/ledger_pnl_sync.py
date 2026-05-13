"""LedgerPnLSync — Ledger 의 realized PnL 을 LiveExecutor.update_realized_pnl 에
주기적으로 sync.

배경:
- LiveExecutor 의 realized_pnl_today_krw 는 외부 feed (broker reconcile / ledger
  aggregator 등) 가 update_realized_pnl 로 주입해야 함.
- 본 모듈 = MockFillSimulator (cycle 23) 가 ledger 채워주는 흐름과 결합 → 매 N초
  ledger 의 누적 realized PnL 합산 → executor 에 주입.
- Risk.daily_loss_limit_krw 가 이 값을 보고 일일 손실 제한 차단.

운영:
- paper_trade 가 LedgerPnLSync(executor, ledger, interval_sec=60) 시작
- 매 60s tick:
  1. aggregate_strategy_pnl(ledger) → 누적 합산
  2. executor.update_realized_pnl(total)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Protocol

from ks_ws.storage.ledger import Ledger
from ks_ws.storage.strategy_pnl import total_realized_pnl_krw

log = logging.getLogger("ks_ws.sources.ledger_pnl_sync")


class _ExecutorLike(Protocol):
    def update_realized_pnl(self, krw: int) -> None: ...


class LedgerPnLSync:
    def __init__(
        self,
        executor: _ExecutorLike,
        ledger: Ledger,
        *,
        interval_sec: float = 60.0,
    ) -> None:
        if interval_sec <= 0:
            raise ValueError("interval_sec must be positive")
        self._executor = executor
        self._ledger = ledger
        self.interval_sec = interval_sec
        self._task: asyncio.Task[None] | None = None
        self.sync_count = 0
        self.last_pnl_krw = 0

    def step(self) -> int:
        pnl = total_realized_pnl_krw(self._ledger)
        self._executor.update_realized_pnl(pnl)
        self.last_pnl_krw = pnl
        self.sync_count += 1
        if pnl != 0:
            log.info("ledger pnl sync: realized_pnl_today=%+,d KRW", pnl)
        return pnl

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
