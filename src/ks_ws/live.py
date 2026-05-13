"""LiveExecutor — receives OrderIntents from the bus, runs them through
Risk, and submits via OrderRouter.

The live counterpart to BacktestDriver. The Allocator already publishes
OrderIntents on the bus; LiveExecutor is what listens in live mode and
gets them to the broker — same input topic, different fulfillment.

State model (v1): every successfully submitted order is treated as
filled immediately at intent.limit_price for the purpose of the
internal position tracker. That's an approximation — real fills come
back asynchronously via WS and may differ in price / quantity. For
real broker reconciliation, plug a fill listener into a future
on_fill callback.

Realized PnL is NOT inferred from submissions in v1 (no fill price,
no average-cost lots). The Risk daily-loss circuit is therefore quiet
until the caller explicitly feeds it via update_realized_pnl(), which
is the integration point for external accounting / broker reconcile.

Two run modes share the same _handle dispatch:

- step(): synchronous drain of all queued intents. For tests and tight
  integration scripts.
- start() / stop(): continuous async mode. The supported live path.
"""

import asyncio
import contextlib
import logging

from ks_ws.bus import EventBus, Subscription
from ks_ws.domain import OrderIntent, Side
from ks_ws.orders import OrderRouter, SubmittedOrder
from ks_ws.risk import EnhancedRisk, Risk
from ks_ws.storage.ledger import Ledger

log = logging.getLogger("ks_ws.live")


class LiveExecutor:
    def __init__(
        self,
        bus: EventBus,
        risk: Risk | EnhancedRisk,
        router: OrderRouter,
        ledger: Ledger | None = None,
    ) -> None:
        self._bus = bus
        self._risk = risk
        self._router = router
        self._ledger = ledger
        self._sub: Subscription[OrderIntent] | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False

        # In-memory net position per symbol. Used as Risk context only.
        # This is *submit-based* (optimistic) — every successful submit bumps
        # the count, ignoring whether the broker actually fills. The Ledger
        # carries the fill-based view that broker reconciliation eventually
        # populates via apply_fill_event().
        self._positions: dict[str, int] = {}
        # External accounting feeds this in via update_realized_pnl().
        self._realized_pnl_today_krw = 0

        # Audit trails — useful for tests, monitoring, end-of-day summaries.
        self._submitted: list[SubmittedOrder] = []
        self._rejected_by_risk: list[OrderIntent] = []
        self._failed_submits: list[OrderIntent] = []

    @property
    def running(self) -> bool:
        return self._running

    @property
    def positions(self) -> dict[str, int]:
        return dict(self._positions)

    @property
    def submitted(self) -> list[SubmittedOrder]:
        return list(self._submitted)

    @property
    def rejected_by_risk(self) -> list[OrderIntent]:
        return list(self._rejected_by_risk)

    @property
    def failed_submits(self) -> list[OrderIntent]:
        return list(self._failed_submits)

    @property
    def realized_pnl_today_krw(self) -> int:
        return self._realized_pnl_today_krw

    def update_realized_pnl(self, krw: int) -> None:
        """Called by external accounting (broker reconcile, ledger) to
        feed realized PnL into the Risk daily-loss circuit."""
        self._realized_pnl_today_krw = krw

    def setup(self) -> None:
        """Idempotent. Subscribes to OrderIntent so the dispatch loops
        have something to consume."""
        if self._sub is not None:
            return
        self._sub = self._bus.subscribe(OrderIntent)

    def step(self) -> int:
        """Drain queued intents synchronously and dispatch. Returns the
        number processed."""
        if self._sub is None:
            self.setup()
        assert self._sub is not None
        count = 0
        while self._sub.qsize() > 0:
            try:
                intent = self._sub.get_nowait()
            except StopAsyncIteration:
                break
            self._handle(intent)
            count += 1
        return count

    async def start(self) -> None:
        """Continuous mode: spin up a task that consumes the subscription
        forever (until stop())."""
        if self._running:
            return
        if self._sub is None:
            self.setup()
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Idempotent. Closes the subscription (drains the async-for via
        sentinel), awaits the dispatch task."""
        if not self._running:
            return
        self._running = False
        if self._sub is not None:
            self._sub.close()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def _loop(self) -> None:
        assert self._sub is not None
        async for intent in self._sub:
            self._handle(intent)

    def _handle(self, intent: OrderIntent) -> None:
        approved = self._risk.check(
            intent,
            current_position=self._positions.get(intent.symbol, 0),
            realized_pnl_today_krw=self._realized_pnl_today_krw,
        )
        if approved is None:
            self._rejected_by_risk.append(intent)
            log.info(
                "intent rejected by risk: %s %s qty=%d",
                intent.symbol,
                intent.side,
                intent.quantity,
            )
            return
        try:
            result = self._router.submit(approved)
        except Exception:
            log.exception("router.submit raised for %s", approved.symbol)
            self._failed_submits.append(approved)
            return
        self._submitted.append(result)
        if self._ledger is not None:
            try:
                self._ledger.record_order(result)
            except Exception:
                log.exception("ledger.record_order raised for %s", result.order_id)
        # Publish SubmittedOrder so external fill simulators (mock environments)
        # can react. Real-broker mode uses apply_fill_event from broker reconcile.
        with contextlib.suppress(Exception):
            self._bus.publish(result)
        self._update_position(approved)

    def _update_position(self, intent: OrderIntent) -> None:
        sym = intent.symbol
        current = self._positions.get(sym, 0)
        if intent.side == Side.BUY:
            self._positions[sym] = current + intent.quantity
        else:
            sold = min(current, intent.quantity)
            self._positions[sym] = current - sold

    def apply_fill_event(
        self,
        *,
        order_id: str,
        symbol: str,
        side: Side,
        quantity: int,
        price: int,
    ) -> None:
        """Hook for an external fill source (broker WS / poll / manual reconcile)
        to inform the executor of an actual fill. Forwards to the Ledger if
        configured so the fills + positions tables stay accurate.

        Note: this does NOT update the internal optimistic _positions tracker —
        that one is submit-based on purpose. Risk reads it for live capping.
        Fill-based position tracking lives in the Ledger.
        """
        if self._ledger is None:
            log.info(
                "fill event without ledger: order_id=%s symbol=%s qty=%d @ %d",
                order_id,
                symbol,
                quantity,
                price,
            )
            return
        self._ledger.apply_fill(
            order_id=order_id, symbol=symbol, side=side, quantity=quantity, price=price
        )
