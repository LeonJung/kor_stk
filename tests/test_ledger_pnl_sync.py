"""LedgerPnLSync — Ledger → LiveExecutor.update_realized_pnl."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ks_ws.domain import OrderIntent, Side
from ks_ws.orders import SubmittedOrder
from ks_ws.sources.ledger_pnl_sync import LedgerPnLSync
from ks_ws.storage.ledger import Ledger


class _StubExecutor:
    def __init__(self) -> None:
        self.last_pnl = 0
        self.update_count = 0

    def update_realized_pnl(self, krw: int) -> None:
        self.last_pnl = krw
        self.update_count += 1


def _seed_buy_sell(ledger: Ledger, *, buy_price: int, sell_price: int,
                   qty: int = 1, symbol: str = "A") -> None:
    base = datetime.now(UTC)
    buy_intent = OrderIntent(symbol=symbol, side=Side.BUY, quantity=qty,
                             timestamp=base, sources=("test",))
    sell_intent = OrderIntent(symbol=symbol, side=Side.SELL, quantity=qty,
                              timestamp=base, sources=("test",))
    ledger.record_order(SubmittedOrder(order_id="b1", intent=buy_intent,
                                       submitted_at=base))
    ledger.apply_fill(order_id="b1", symbol=symbol, side=Side.BUY,
                      quantity=qty, price=buy_price)
    ledger.record_order(SubmittedOrder(order_id="s1", intent=sell_intent,
                                       submitted_at=base))
    ledger.apply_fill(order_id="s1", symbol=symbol, side=Side.SELL,
                      quantity=qty, price=sell_price)


def test_step_updates_executor_with_profit(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    _seed_buy_sell(ledger, buy_price=100, sell_price=110, qty=10)
    # Realized = (110 - 100) * 10 = 100
    exe = _StubExecutor()
    sync = LedgerPnLSync(exe, ledger, interval_sec=60.0)
    pnl = sync.step()
    assert pnl == 100
    assert exe.last_pnl == 100
    assert exe.update_count == 1
    ledger.close()


def test_step_updates_with_loss(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    _seed_buy_sell(ledger, buy_price=110, sell_price=100, qty=5)
    # Realized = (100 - 110) * 5 = -50
    exe = _StubExecutor()
    sync = LedgerPnLSync(exe, ledger)
    pnl = sync.step()
    assert pnl == -50
    assert exe.last_pnl == -50
    ledger.close()


def test_step_with_no_fills(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    exe = _StubExecutor()
    sync = LedgerPnLSync(exe, ledger)
    pnl = sync.step()
    assert pnl == 0
    assert exe.update_count == 1
    ledger.close()


def test_invalid_interval_raises(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    with pytest.raises(ValueError):
        LedgerPnLSync(_StubExecutor(), ledger, interval_sec=0)
    ledger.close()


def test_async_loop_invokes_step(tmp_path: Path) -> None:
    async def _run() -> None:
        ledger = Ledger(tmp_path / "ledger.sqlite")
        _seed_buy_sell(ledger, buy_price=100, sell_price=120, qty=2)
        exe = _StubExecutor()
        sync = LedgerPnLSync(exe, ledger, interval_sec=0.05)
        await sync.start()
        try:
            await asyncio.sleep(0.15)
            # At least 1 step should have run
            assert exe.update_count >= 1
            assert exe.last_pnl == 40  # (120-100)*2
        finally:
            await sync.stop()
        ledger.close()

    asyncio.run(_run())
