from datetime import UTC, datetime

from ks_ws.domain import OrderIntent, Side
from ks_ws.orders import SubmittedOrder
from ks_ws.storage.ledger import Ledger


def _submitted(order_id="mock-1", symbol="005930", side=Side.BUY, qty=10, price=70_000):
    intent = OrderIntent(
        symbol=symbol,
        side=side,
        quantity=qty,
        order_type="limit",
        limit_price=price,
        timestamp=datetime.now(UTC),
        sources=("alpha", "beta"),
    )
    return SubmittedOrder(
        order_id=order_id,
        intent=intent,
        submitted_at=datetime.now(UTC),
    )


def test_create_and_close(tmp_path):
    db = tmp_path / "ledger.sqlite"
    ledger = Ledger(db)
    assert db.exists()
    ledger.close()


def test_record_and_list_order(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    submitted = _submitted()
    ledger.record_order(submitted)
    rows = ledger.list_orders()
    assert len(rows) == 1
    assert rows[0]["order_id"] == "mock-1"
    assert rows[0]["symbol"] == "005930"
    assert rows[0]["quantity"] == 10
    assert rows[0]["sources"] == "alpha,beta"
    ledger.close()


def test_record_order_idempotent_on_same_id(tmp_path):
    """INSERT OR REPLACE — a re-recorded order updates rather than duplicates."""
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.record_order(_submitted(qty=10))
    ledger.record_order(_submitted(qty=20))
    rows = ledger.list_orders()
    assert len(rows) == 1
    assert rows[0]["quantity"] == 20
    ledger.close()


def test_list_orders_by_symbol(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.record_order(_submitted(order_id="a", symbol="005930"))
    ledger.record_order(_submitted(order_id="b", symbol="000660"))
    samsung = ledger.list_orders(symbol="005930")
    assert len(samsung) == 1
    assert samsung[0]["order_id"] == "a"
    ledger.close()


def test_record_and_list_fills(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.record_order(_submitted())
    fid = ledger.record_fill(
        order_id="mock-1", symbol="005930", side=Side.BUY, quantity=5, price=70_100
    )
    assert fid > 0
    fills = ledger.list_fills(order_id="mock-1")
    assert len(fills) == 1
    assert fills[0]["quantity"] == 5
    assert fills[0]["price"] == 70_100
    ledger.close()


def test_apply_fill_updates_position_avg_cost(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.record_order(_submitted())
    ledger.apply_fill(order_id="mock-1", symbol="005930", side=Side.BUY, quantity=10, price=70_000)
    ledger.apply_fill(order_id="mock-1", symbol="005930", side=Side.BUY, quantity=10, price=72_000)
    pos = ledger.get_position("005930")
    assert pos is not None
    assert pos["quantity"] == 20
    assert pos["average_cost"] == 71_000.0  # weighted
    ledger.close()


def test_apply_fill_sell_reduces_quantity(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.record_order(_submitted())
    ledger.apply_fill(order_id="mock-1", symbol="005930", side=Side.BUY, quantity=10, price=70_000)
    ledger.apply_fill(order_id="mock-1", symbol="005930", side=Side.SELL, quantity=4, price=72_000)
    pos = ledger.get_position("005930")
    assert pos is not None
    assert pos["quantity"] == 6
    # Sells don't change avg cost on remaining; PnL realized externally.
    assert pos["average_cost"] == 70_000.0
    ledger.close()


def test_apply_fill_full_close_resets_avg_cost(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.record_order(_submitted())
    ledger.apply_fill(order_id="mock-1", symbol="005930", side=Side.BUY, quantity=10, price=70_000)
    ledger.apply_fill(order_id="mock-1", symbol="005930", side=Side.SELL, quantity=10, price=72_000)
    pos = ledger.get_position("005930")
    assert pos is not None
    assert pos["quantity"] == 0
    assert pos["average_cost"] == 0.0
    ledger.close()


def test_persistence_across_reopen(tmp_path):
    """Close one Ledger handle, open a fresh one on the same file —
    everything should be visible."""
    db = tmp_path / "ledger.sqlite"
    a = Ledger(db)
    a.record_order(_submitted())
    a.apply_fill(order_id="mock-1", symbol="005930", side=Side.BUY, quantity=10, price=70_000)
    a.close()

    b = Ledger(db)
    assert len(b.list_orders()) == 1
    assert b.get_position("005930")["quantity"] == 10
    b.close()


def test_build_intent_from_order_row_round_trips(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    submitted = _submitted()
    ledger.record_order(submitted)
    row = ledger.list_orders()[0]
    rebuilt = ledger.build_intent_from_order_row(row)
    assert rebuilt.symbol == submitted.intent.symbol
    assert rebuilt.side == submitted.intent.side
    assert rebuilt.quantity == submitted.intent.quantity
    assert rebuilt.order_type == submitted.intent.order_type
    assert rebuilt.limit_price == submitted.intent.limit_price
    assert rebuilt.sources == submitted.intent.sources
    ledger.close()
