"""MockFillSimulator — paper_trade mock fill 우회."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ks_ws.bus import EventBus
from ks_ws.domain import OrderIntent, Side, Tick
from ks_ws.orders import SubmittedOrder
from ks_ws.sources.mock_fill_simulator import MockFillSimulator


class _StubExecutor:
    def __init__(self) -> None:
        self.fills: list[dict] = []

    def apply_fill_event(self, *, order_id: str, symbol: str,
                         side: Side, quantity: int, price: int) -> None:
        self.fills.append({
            "order_id": order_id, "symbol": symbol, "side": side,
            "quantity": quantity, "price": price,
        })


def _intent(symbol: str = "005930", side: Side = Side.BUY,
            qty: int = 1) -> OrderIntent:
    return OrderIntent(
        symbol=symbol, side=side, quantity=qty,
        timestamp=datetime.now(UTC), sources=("test",),
    )


def _order(intent: OrderIntent, order_id: str = "o-1") -> SubmittedOrder:
    return SubmittedOrder(
        order_id=order_id, intent=intent,
        submitted_at=datetime.now(UTC),
    )


def test_effective_price_buy() -> None:
    bus = EventBus()
    sim = MockFillSimulator(bus, _StubExecutor(),
                            commission_bps=1.5, sell_tax_bps=18.0, slippage_bps=0)
    assert sim._effective_price(Side.BUY, 10000) == 10001
    assert sim._effective_price(Side.SELL, 10000) == 9980


def test_async_fill_after_order_publish() -> None:
    async def _run() -> None:
        bus = EventBus()
        exe = _StubExecutor()
        sim = MockFillSimulator(bus, exe, fill_delay_sec=0)
        await sim.start()
        try:
            tick = Tick(symbol="005930", price=10000, volume=10,
                        timestamp=datetime.now(UTC))
            bus.publish(tick)
            await asyncio.sleep(0.02)
            order = _order(_intent())
            bus.publish(order)
            await asyncio.sleep(0.05)
            assert len(exe.fills) == 1
            assert exe.fills[0]["price"] == 10001  # BUY + commission
            assert exe.fills[0]["symbol"] == "005930"
        finally:
            await sim.stop()

    asyncio.run(_run())


def test_no_fill_when_no_tick() -> None:
    async def _run() -> None:
        bus = EventBus()
        exe = _StubExecutor()
        sim = MockFillSimulator(bus, exe)
        await sim.start()
        try:
            # No Tick first — order has no price reference
            bus.publish(_order(_intent()))
            await asyncio.sleep(0.05)
            assert len(exe.fills) == 0
        finally:
            await sim.stop()

    asyncio.run(_run())


def test_sell_fill_uses_lower_effective_price() -> None:
    async def _run() -> None:
        bus = EventBus()
        exe = _StubExecutor()
        sim = MockFillSimulator(bus, exe)
        await sim.start()
        try:
            bus.publish(Tick(symbol="A", price=10000, volume=10,
                             timestamp=datetime.now(UTC)))
            await asyncio.sleep(0.02)
            bus.publish(_order(_intent(symbol="A", side=Side.SELL)))
            await asyncio.sleep(0.05)
            assert exe.fills[0]["price"] == 9980
        finally:
            await sim.stop()

    asyncio.run(_run())


def test_invalid_fill_delay_raises() -> None:
    with pytest.raises(ValueError):
        MockFillSimulator(EventBus(), _StubExecutor(), fill_delay_sec=-1)


def test_zero_cost_returns_mid() -> None:
    sim = MockFillSimulator(
        EventBus(), _StubExecutor(),
        commission_bps=0, sell_tax_bps=0, slippage_bps=0,
    )
    assert sim._effective_price(Side.BUY, 10000) == 10000
    assert sim._effective_price(Side.SELL, 10000) == 10000
