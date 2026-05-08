import asyncio
from datetime import UTC, datetime

from ks_ws.bus import EventBus
from ks_ws.domain import OrderIntent, Side
from ks_ws.live import LiveExecutor
from ks_ws.orders import MockOrderRouter, OrderRouter, SubmittedOrder
from ks_ws.risk import Risk


def _intent(side=Side.BUY, qty=10, symbol="005930", price=70_000):
    return OrderIntent(
        symbol=symbol,
        side=side,
        quantity=qty,
        order_type="limit",
        limit_price=price,
        timestamp=datetime.now(UTC),
    )


# step() — synchronous mode --------------------------------------------------


def test_step_processes_pending_intent_via_risk_and_router():
    bus = EventBus()
    router = MockOrderRouter()
    executor = LiveExecutor(bus, Risk(max_position_per_symbol=100), router)
    executor.setup()

    bus.publish(_intent())
    processed = executor.step()

    assert processed == 1
    assert len(router.submitted) == 1
    assert executor.submitted[0].order_id == "mock-1"
    assert executor.positions["005930"] == 10


def test_step_returns_zero_when_nothing_queued():
    executor = LiveExecutor(EventBus(), Risk(), MockOrderRouter())
    assert executor.step() == 0


def test_buy_then_sell_position_tracking():
    bus = EventBus()
    executor = LiveExecutor(bus, Risk(max_position_per_symbol=100), MockOrderRouter())
    executor.setup()

    bus.publish(_intent(side=Side.BUY, qty=30))
    bus.publish(_intent(side=Side.SELL, qty=20))
    executor.step()

    assert executor.positions["005930"] == 10


def test_sell_caps_at_owned_quantity():
    bus = EventBus()
    executor = LiveExecutor(bus, Risk(max_position_per_symbol=100), MockOrderRouter())
    executor.setup()

    bus.publish(_intent(side=Side.BUY, qty=10))
    bus.publish(_intent(side=Side.SELL, qty=50))
    executor.step()

    # Internal tracker: 10 bought, sells happen at requested 50 — but
    # the position math caps at the owned amount, ending at 0 (or
    # left at 0 at minimum). Sells aren't capped by Risk since v1's
    # Risk doesn't gate sells, so the router still gets a 50-qty intent.
    # Position tracker reflects what we actually own.
    assert executor.positions["005930"] == 0


# Risk integration ----------------------------------------------------------


def test_risk_rejection_records_and_skips_router():
    """daily_loss_limit triggered → every intent rejected; router never called."""
    bus = EventBus()
    router = MockOrderRouter()
    executor = LiveExecutor(bus, Risk(daily_loss_limit_krw=1_000_000), router)
    executor.setup()
    executor.update_realized_pnl(-2_000_000)  # below limit

    bus.publish(_intent())
    executor.step()

    assert len(router.submitted) == 0
    assert len(executor.rejected_by_risk) == 1
    assert "005930" not in executor.positions


def test_risk_modifies_quantity_uses_reduced():
    """Risk caps qty=80 against existing position=70 → submit 30 only."""
    bus = EventBus()
    router = MockOrderRouter()
    executor = LiveExecutor(bus, Risk(max_position_per_symbol=100), router)
    executor.setup()

    # Bootstrap to 70 owned
    bus.publish(_intent(qty=70))
    executor.step()
    assert executor.positions["005930"] == 70

    # Try to buy another 80 — Risk caps to 30
    bus.publish(_intent(qty=80))
    executor.step()
    assert executor.positions["005930"] == 100
    assert router.submitted[1].intent.quantity == 30


def test_router_exception_recorded_and_does_not_crash_loop():
    class _RaisingRouter(OrderRouter):
        def submit(self, intent):
            raise RuntimeError("broker down")

    bus = EventBus()
    executor = LiveExecutor(bus, Risk(max_position_per_symbol=100), _RaisingRouter())
    executor.setup()

    bus.publish(_intent())
    executor.step()
    assert len(executor.failed_submits) == 1
    # No position change because submit failed
    assert "005930" not in executor.positions


def test_update_realized_pnl_affects_subsequent_check():
    bus = EventBus()
    executor = LiveExecutor(bus, Risk(daily_loss_limit_krw=1_000_000), MockOrderRouter())
    executor.setup()

    # First intent goes through (no loss)
    bus.publish(_intent())
    executor.step()
    assert len(executor.submitted) == 1

    # Now trigger the circuit
    executor.update_realized_pnl(-1_500_000)
    bus.publish(_intent())
    executor.step()
    assert len(executor.submitted) == 1  # unchanged
    assert len(executor.rejected_by_risk) == 1


# Async start / stop --------------------------------------------------------


def test_continuous_mode_processes_published_intent():
    async def run():
        bus = EventBus()
        router = MockOrderRouter()
        executor = LiveExecutor(bus, Risk(max_position_per_symbol=100), router)
        await executor.start()
        bus.publish(_intent())
        for _ in range(5):
            await asyncio.sleep(0)
        await executor.stop()
        return router.submitted

    submitted = asyncio.run(run())
    assert len(submitted) == 1


def test_start_idempotent_and_stop_idempotent():
    async def run():
        executor = LiveExecutor(EventBus(), Risk(), MockOrderRouter())
        await executor.start()
        await executor.start()  # second is no-op
        running_after_double_start = executor.running
        await executor.stop()
        await executor.stop()
        return running_after_double_start, executor.running

    after_double, after_stop = asyncio.run(run())
    assert after_double is True
    assert after_stop is False


# Audit / read-only views ---------------------------------------------------


def test_positions_returns_a_copy():
    bus = EventBus()
    executor = LiveExecutor(bus, Risk(max_position_per_symbol=100), MockOrderRouter())
    executor.setup()
    bus.publish(_intent())
    executor.step()

    snapshot = executor.positions
    snapshot["XXX"] = 999
    assert "XXX" not in executor.positions


def test_submitted_returns_a_copy():
    bus = EventBus()
    executor = LiveExecutor(bus, Risk(max_position_per_symbol=100), MockOrderRouter())
    executor.setup()
    bus.publish(_intent())
    executor.step()

    snap = executor.submitted
    snap.clear()
    assert len(executor.submitted) == 1


# SubmittedOrder typed correctly --------------------------------------------


def test_submitted_orders_are_typed():
    bus = EventBus()
    executor = LiveExecutor(bus, Risk(max_position_per_symbol=100), MockOrderRouter())
    executor.setup()
    bus.publish(_intent())
    executor.step()

    assert all(isinstance(o, SubmittedOrder) for o in executor.submitted)


# Ledger integration --------------------------------------------------------


def test_submit_records_order_in_ledger(tmp_path):
    from ks_ws.storage.ledger import Ledger

    ledger = Ledger(tmp_path / "ledger.sqlite")
    bus = EventBus()
    executor = LiveExecutor(
        bus, Risk(max_position_per_symbol=100), MockOrderRouter(), ledger=ledger
    )
    executor.setup()

    bus.publish(_intent(qty=5))
    executor.step()

    rows = ledger.list_orders()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "005930"
    assert rows[0]["quantity"] == 5
    ledger.close()


def test_risk_rejection_does_not_record_in_ledger(tmp_path):
    from ks_ws.storage.ledger import Ledger

    ledger = Ledger(tmp_path / "ledger.sqlite")
    bus = EventBus()
    executor = LiveExecutor(
        bus, Risk(daily_loss_limit_krw=1_000_000), MockOrderRouter(), ledger=ledger
    )
    executor.setup()
    executor.update_realized_pnl(-2_000_000)  # past limit

    bus.publish(_intent())
    executor.step()

    assert ledger.list_orders() == []
    ledger.close()


def test_apply_fill_event_writes_to_ledger(tmp_path):
    from ks_ws.storage.ledger import Ledger

    ledger = Ledger(tmp_path / "ledger.sqlite")
    bus = EventBus()
    executor = LiveExecutor(
        bus, Risk(max_position_per_symbol=100), MockOrderRouter(), ledger=ledger
    )
    executor.setup()

    bus.publish(_intent(qty=10))
    executor.step()

    # Simulate a broker fill at 70_500
    executor.apply_fill_event(
        order_id="mock-1",
        symbol="005930",
        side=Side.BUY,
        quantity=10,
        price=70_500,
    )
    pos = ledger.get_position("005930")
    assert pos is not None
    assert pos["quantity"] == 10
    assert pos["average_cost"] == 70_500.0
    assert len(ledger.list_fills()) == 1
    ledger.close()


def test_apply_fill_without_ledger_only_logs(caplog):
    bus = EventBus()
    executor = LiveExecutor(bus, Risk(max_position_per_symbol=100), MockOrderRouter())
    with caplog.at_level("INFO", logger="ks_ws.live"):
        executor.apply_fill_event(
            order_id="mock-1", symbol="005930", side=Side.BUY, quantity=1, price=70_000
        )
    assert any("fill event without ledger" in m for m in caplog.messages)


def test_ledger_failure_does_not_kill_dispatch(tmp_path):
    """Ledger.record_order raising is logged, not propagated."""
    from ks_ws.storage.ledger import Ledger

    class _BrokenLedger(Ledger):
        def record_order(self, _submitted):
            raise RuntimeError("disk full")

    ledger = _BrokenLedger(tmp_path / "ledger.sqlite")
    bus = EventBus()
    router = MockOrderRouter()
    executor = LiveExecutor(bus, Risk(max_position_per_symbol=100), router, ledger=ledger)
    executor.setup()

    bus.publish(_intent())
    executor.step()

    # Submit still happened, ledger error swallowed
    assert len(router.submitted) == 1
    ledger.close()
