"""OrderRouter — submits OrderIntents to a broker.

A concrete ``KisOrderRouter`` calling ``/uapi/.../order`` will arrive
once an APP_KEY is available. For now ``MockOrderRouter`` records every
submission to memory so tests, mock-live dry runs, and pre-key
development can verify the path end-to-end without a network call.

The router is the *submission* point — fill semantics belong elsewhere
(BacktestDriver simulates fills against bars; a future live executor
will receive fills via WS or polling REST). This separation keeps the
router itself a thin interface.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime

from ks_ws.domain import OrderIntent


@dataclass(frozen=True)
class SubmittedOrder:
    order_id: str
    intent: OrderIntent
    submitted_at: datetime


class OrderRouter(ABC):
    @abstractmethod
    def submit(self, intent: OrderIntent) -> SubmittedOrder:
        """Submit an order to the broker. Returns SubmittedOrder with the
        broker's order ID. Implementations decide whether to block on
        confirmation or return as soon as the broker accepts the request.
        """


class MockOrderRouter(OrderRouter):
    """Records every submitted intent in memory. No fill simulation —
    pair with a backtest driver or future live executor for that.
    """

    def __init__(self) -> None:
        self._submitted: list[SubmittedOrder] = []
        self._counter = 0

    def submit(self, intent: OrderIntent) -> SubmittedOrder:
        self._counter += 1
        order = SubmittedOrder(
            order_id=f"mock-{self._counter}",
            intent=intent,
            submitted_at=datetime.now(UTC),
        )
        self._submitted.append(order)
        return order

    @property
    def submitted(self) -> list[SubmittedOrder]:
        return list(self._submitted)

    def clear(self) -> None:
        self._submitted.clear()
        self._counter = 0
