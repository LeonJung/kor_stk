"""BrokerAdapter Protocol — Phase 2.3 sketch."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ks_ws.domain import OrderIntent, Side
from ks_ws.orders import SubmittedOrder
from ks_ws.orders_broker_adapter import (
    AccountBalance,
    BrokerAdapter,
    DaishinAdapter,
    KisAdapter,
    OrderStatus,
)


def test_order_status_dataclass() -> None:
    s = OrderStatus(
        order_id="o1", symbol="A", submitted_at=datetime.now(UTC),
        state="pending", filled_qty=0, avg_fill_price=0, remaining_qty=10,
    )
    assert s.state == "pending"


def test_account_balance_dataclass() -> None:
    b = AccountBalance(cash_krw=1_000_000, stock_value_krw=500_000,
                       total_value_krw=1_500_000)
    assert b.total_value_krw == 1_500_000


def test_kis_adapter_name() -> None:
    # Don't actually construct KisOrderRouter (would need settings)
    # Just verify class attrs
    assert KisAdapter.name == "kis"


def test_daishin_adapter_name() -> None:
    assert DaishinAdapter.name == "daishin"


def test_daishin_adapter_methods_raise() -> None:
    ad = DaishinAdapter()
    intent = OrderIntent(symbol="A", side=Side.BUY, quantity=1,
                         timestamp=datetime.now(UTC), sources=("t",))
    with pytest.raises(NotImplementedError):
        ad.submit(intent)
    with pytest.raises(NotImplementedError):
        ad.cancel("o1")
    with pytest.raises(NotImplementedError):
        ad.get_status("o1")
    with pytest.raises(NotImplementedError):
        ad.account_balance()


def test_broker_adapter_is_protocol() -> None:
    """Protocol typing only — runtime check 불가능, 하지만 attr 존재 검증."""
    assert hasattr(BrokerAdapter, "submit")
    assert hasattr(BrokerAdapter, "cancel")
    assert hasattr(BrokerAdapter, "get_status")
    assert hasattr(BrokerAdapter, "account_balance")


class _StubAdapter:
    """Demonstrate Protocol satisfaction without inheritance."""
    name = "stub"

    def __init__(self) -> None:
        self.submitted: list[OrderIntent] = []

    def submit(self, intent: OrderIntent) -> SubmittedOrder:
        self.submitted.append(intent)
        return SubmittedOrder(
            order_id=f"stub-{len(self.submitted)}",
            intent=intent, submitted_at=datetime.now(UTC),
        )

    def cancel(self, order_id: str) -> bool:
        return True

    def get_status(self, order_id: str) -> OrderStatus | None:
        return None

    def account_balance(self) -> AccountBalance | None:
        return AccountBalance(cash_krw=1000, stock_value_krw=0, total_value_krw=1000)


def test_stub_adapter_satisfies_protocol() -> None:
    """Duck-typing 으로 Protocol 만족 검증."""
    adapter: BrokerAdapter = _StubAdapter()  # type: ignore[assignment]
    intent = OrderIntent(symbol="A", side=Side.BUY, quantity=1,
                         timestamp=datetime.now(UTC), sources=("t",))
    order = adapter.submit(intent)
    assert order.order_id == "stub-1"
    assert adapter.cancel("o1") is True
    assert adapter.get_status("o1") is None
    bal = adapter.account_balance()
    assert bal is not None
    assert bal.cash_krw == 1000
