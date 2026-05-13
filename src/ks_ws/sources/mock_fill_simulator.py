"""MockFillSimulator — KIS mock 이 실제 fill event 안 줄 때 우회.

배경:
- 2026-05-13 paper_trade: 75 orders 제출 → ledger fills=0. KIS mock 이 fill
  notification 안 보냄. 모든 LiveExecutor.apply_fill_event 호출 X → ledger
  fills 테이블 empty → realized PnL 계산 불가 → review_log 도 mock fill 의존하지
  않지만 ledger 기반 보고서 부정확.

해결:
- 본 simulator 가 OrderIntent + Tick 을 subscribe
- 매 OrderIntent 제출 직후 (또는 다음 tick), 그 종목의 최근 tick price 로
  apply_fill_event 호출 → ledger 정상 채워짐
- TickReplayDriver 의 cost 모델 (commission + sell_tax + slippage) 동일 적용
- paper_trade 의 정확한 PnL 추적 가능

설계:
- ``MockFillSimulator(bus, executor, *, commission_bps, sell_tax_bps, slippage_bps,
  fill_delay_sec=0)``
- bus subscribe(SubmittedOrder) + bus subscribe(Tick)
- Tick 도착 시 _last_price[symbol] 갱신
- SubmittedOrder 도착 시 (또는 그 직후 fill_delay_sec 후) _last_price 로 fill
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Protocol

from ks_ws.bus import EventBus
from ks_ws.domain import Side, Tick
from ks_ws.orders import SubmittedOrder

log = logging.getLogger("ks_ws.sources.mock_fill_simulator")


class _ExecutorLike(Protocol):
    def apply_fill_event(
        self, *, order_id: str, symbol: str, side: Side, quantity: int, price: int,
    ) -> None: ...


class MockFillSimulator:
    def __init__(
        self,
        bus: EventBus,
        executor: _ExecutorLike,
        *,
        commission_bps: float = 1.5,
        sell_tax_bps: float = 18.0,
        slippage_bps: float = 0.0,
        fill_delay_sec: float = 0.0,
    ) -> None:
        if fill_delay_sec < 0:
            raise ValueError("fill_delay_sec must be non-negative")
        self._bus = bus
        self._executor = executor
        self.commission_bps = commission_bps
        self.sell_tax_bps = sell_tax_bps
        self.slippage_bps = slippage_bps
        self.fill_delay = fill_delay_sec
        self._last_price: dict[str, int] = {}
        self._tick_sub = None
        self._order_sub = None
        self._tick_task = None
        self._order_task = None
        self.fills_simulated = 0

    def _effective_price(self, side: Side, mid: int) -> int:
        bps = self.commission_bps + self.slippage_bps
        if side == Side.SELL:
            bps += self.sell_tax_bps
            return max(1, int(mid * (1 - bps / 10000)))
        return max(1, int(mid * (1 + bps / 10000)))

    async def start(self) -> None:
        if self._tick_task is not None:
            return
        self._tick_sub = self._bus.subscribe(Tick, maxsize=200_000)
        self._order_sub = self._bus.subscribe(SubmittedOrder, maxsize=10_000)
        self._tick_task = asyncio.create_task(self._tick_loop())
        self._order_task = asyncio.create_task(self._order_loop())

    async def stop(self) -> None:
        for task in (self._tick_task, self._order_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._tick_task = None
        self._order_task = None
        if self._tick_sub is not None:
            self._tick_sub.close()
            self._tick_sub = None
        if self._order_sub is not None:
            self._order_sub.close()
            self._order_sub = None

    async def _tick_loop(self) -> None:
        try:
            async for tick in self._tick_sub:
                self._last_price[tick.symbol] = tick.price
        except asyncio.CancelledError:
            pass

    async def _order_loop(self) -> None:
        try:
            async for order in self._order_sub:
                if self.fill_delay > 0:
                    await asyncio.sleep(self.fill_delay)
                self._fill(order)
        except asyncio.CancelledError:
            pass

    def _fill(self, order: SubmittedOrder) -> None:
        intent = order.intent
        mid = self._last_price.get(intent.symbol)
        if mid is None:
            log.warning(
                "no price for %s — cannot simulate fill (order_id=%s)",
                intent.symbol, order.order_id,
            )
            return
        eff = self._effective_price(intent.side, mid)
        try:
            self._executor.apply_fill_event(
                order_id=order.order_id, symbol=intent.symbol,
                side=intent.side, quantity=intent.quantity, price=eff,
            )
        except Exception:
            log.exception("apply_fill_event failed order_id=%s", order.order_id)
            return
        self.fills_simulated += 1
        log.info(
            "mock fill: %s %s qty=%d @ %d (mid=%d, cost_bps=%.1f)",
            intent.symbol, intent.side.value, intent.quantity, eff, mid,
            self.commission_bps + self.slippage_bps
            + (self.sell_tax_bps if intent.side == Side.SELL else 0),
        )
