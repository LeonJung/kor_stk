"""Tests for per-strategy PnL aggregation."""

from datetime import UTC, datetime

from ks_ws.domain import OrderIntent, Side
from ks_ws.orders import SubmittedOrder
from ks_ws.storage.ledger import Ledger
from ks_ws.storage.strategy_pnl import aggregate_strategy_pnl


def _record_round_trip(
    ledger: Ledger,
    *,
    symbol: str,
    qty: int,
    buy_price: int,
    sell_price: int,
    buy_strategy: str,
    sell_strategy: str | None = None,
    order_id_prefix: str = "ord",
) -> None:
    """Helper: submit BUY then SELL, both filled at given prices."""
    sell_strategy = sell_strategy or buy_strategy
    now = datetime.now(UTC)
    buy_intent = OrderIntent(
        symbol=symbol,
        side=Side.BUY,
        quantity=qty,
        timestamp=now,
        sources=(buy_strategy,),
    )
    buy_order = SubmittedOrder(
        order_id=f"{order_id_prefix}-buy", intent=buy_intent, submitted_at=now
    )
    ledger.record_order(buy_order)
    ledger.apply_fill(
        order_id=buy_order.order_id,
        symbol=symbol,
        side=Side.BUY,
        quantity=qty,
        price=buy_price,
    )
    sell_intent = OrderIntent(
        symbol=symbol,
        side=Side.SELL,
        quantity=qty,
        timestamp=now,
        sources=(sell_strategy,),
    )
    sell_order = SubmittedOrder(
        order_id=f"{order_id_prefix}-sell", intent=sell_intent, submitted_at=now
    )
    ledger.record_order(sell_order)
    ledger.apply_fill(
        order_id=sell_order.order_id,
        symbol=symbol,
        side=Side.SELL,
        quantity=qty,
        price=sell_price,
    )


def test_single_strategy_one_winning_trade(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    _record_round_trip(
        ledger,
        symbol="A005930",
        qty=10,
        buy_price=70000,
        sell_price=71000,
        buy_strategy="pair_follow",
    )
    stats = aggregate_strategy_pnl(ledger)
    assert "pair_follow" in stats
    s = stats["pair_follow"]
    assert s.trades == 1
    assert s.wins == 1
    assert s.losses == 0
    assert s.realized_pnl_krw == 10 * (71000 - 70000)
    assert s.win_rate == 1.0
    assert s.expectancy_krw == 10000.0


def test_losing_trade_negative_pnl(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    _record_round_trip(
        ledger,
        symbol="A005930",
        qty=10,
        buy_price=70000,
        sell_price=69000,
        buy_strategy="pair_follow",
    )
    s = aggregate_strategy_pnl(ledger)["pair_follow"]
    assert s.trades == 1
    assert s.wins == 0
    assert s.losses == 1
    assert s.realized_pnl_krw == -10000.0
    assert s.win_rate == 0.0


def test_multiple_strategies_separate_stats(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    _record_round_trip(
        ledger, symbol="A005930", qty=10, buy_price=70000, sell_price=71000,
        buy_strategy="pair_follow", order_id_prefix="pf1"
    )
    _record_round_trip(
        ledger, symbol="A000660", qty=5, buy_price=100000, sell_price=99000,
        buy_strategy="opening_momentum", order_id_prefix="om1"
    )
    stats = aggregate_strategy_pnl(ledger)
    assert stats["pair_follow"].realized_pnl_krw == 10000.0
    assert stats["opening_momentum"].realized_pnl_krw == -5000.0
    assert stats["pair_follow"].trades == 1
    assert stats["opening_momentum"].trades == 1


def test_pnl_distributed_across_close_sources(tmp_path):
    """When a closing order has multiple contributing strategies, PnL
    splits equally."""
    ledger = Ledger(tmp_path / "ledger.sqlite")
    now = datetime.now(UTC)
    # buy by pair_follow only
    buy_intent = OrderIntent(
        symbol="A005930", side=Side.BUY, quantity=10, timestamp=now,
        sources=("pair_follow",),
    )
    buy = SubmittedOrder(order_id="b1", intent=buy_intent, submitted_at=now)
    ledger.record_order(buy)
    ledger.apply_fill(order_id="b1", symbol="A005930", side=Side.BUY, quantity=10, price=70000)
    # sell shared by two strategies
    sell_intent = OrderIntent(
        symbol="A005930", side=Side.SELL, quantity=10, timestamp=now,
        sources=("pair_follow", "exit_monitor"),
    )
    sell = SubmittedOrder(order_id="s1", intent=sell_intent, submitted_at=now)
    ledger.record_order(sell)
    ledger.apply_fill(order_id="s1", symbol="A005930", side=Side.SELL, quantity=10, price=72000)
    stats = aggregate_strategy_pnl(ledger)
    # 20000 KRW PnL split equally → 10000 each
    assert stats["pair_follow"].realized_pnl_krw == 10000.0
    assert stats["exit_monitor"].realized_pnl_krw == 10000.0


def test_partial_close_fifo_matching(tmp_path):
    """Sell smaller than open lot: partial close, lot reduced."""
    ledger = Ledger(tmp_path / "ledger.sqlite")
    now = datetime.now(UTC)
    buy_intent = OrderIntent(
        symbol="A005930", side=Side.BUY, quantity=10, timestamp=now,
        sources=("pair_follow",),
    )
    buy = SubmittedOrder(order_id="b1", intent=buy_intent, submitted_at=now)
    ledger.record_order(buy)
    ledger.apply_fill(order_id="b1", symbol="A005930", side=Side.BUY, quantity=10, price=70000)
    # sell 4 of 10
    sell_intent = OrderIntent(
        symbol="A005930", side=Side.SELL, quantity=4, timestamp=now,
        sources=("pair_follow",),
    )
    sell = SubmittedOrder(order_id="s1", intent=sell_intent, submitted_at=now)
    ledger.record_order(sell)
    ledger.apply_fill(order_id="s1", symbol="A005930", side=Side.SELL, quantity=4, price=72000)
    s = aggregate_strategy_pnl(ledger)["pair_follow"]
    assert s.trades == 1
    assert s.realized_pnl_krw == 4 * 2000  # 4 shares × 2000 profit


def test_multiple_buy_lots_fifo(tmp_path):
    """Two buys at different prices, single sell uses oldest lot first."""
    ledger = Ledger(tmp_path / "ledger.sqlite")
    now = datetime.now(UTC)
    for i, (qty, price) in enumerate([(5, 70000), (5, 71000)]):
        intent = OrderIntent(
            symbol="A005930", side=Side.BUY, quantity=qty, timestamp=now,
            sources=("pair_follow",),
        )
        order = SubmittedOrder(order_id=f"b{i}", intent=intent, submitted_at=now)
        ledger.record_order(order)
        ledger.apply_fill(
            order_id=f"b{i}", symbol="A005930", side=Side.BUY, quantity=qty, price=price
        )
    sell_intent = OrderIntent(
        symbol="A005930", side=Side.SELL, quantity=8, timestamp=now,
        sources=("pair_follow",),
    )
    sell = SubmittedOrder(order_id="s1", intent=sell_intent, submitted_at=now)
    ledger.record_order(sell)
    ledger.apply_fill(order_id="s1", symbol="A005930", side=Side.SELL, quantity=8, price=72000)
    s = aggregate_strategy_pnl(ledger)["pair_follow"]
    # FIFO: 5 @ 70000 closed (profit 5 × 2000 = 10000), 3 @ 71000 closed (profit 3 × 1000 = 3000)
    assert s.realized_pnl_krw == 10000 + 3000


def test_empty_ledger_returns_empty(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    assert aggregate_strategy_pnl(ledger) == {}


def test_unknown_source_when_order_has_no_strategy(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    now = datetime.now(UTC)
    buy_intent = OrderIntent(symbol="A005930", side=Side.BUY, quantity=10, timestamp=now)
    buy = SubmittedOrder(order_id="b1", intent=buy_intent, submitted_at=now)
    ledger.record_order(buy)
    ledger.apply_fill(order_id="b1", symbol="A005930", side=Side.BUY, quantity=10, price=70000)
    sell_intent = OrderIntent(symbol="A005930", side=Side.SELL, quantity=10, timestamp=now)
    sell = SubmittedOrder(order_id="s1", intent=sell_intent, submitted_at=now)
    ledger.record_order(sell)
    ledger.apply_fill(order_id="s1", symbol="A005930", side=Side.SELL, quantity=10, price=71000)
    stats = aggregate_strategy_pnl(ledger)
    assert "(unknown)" in stats
    assert stats["(unknown)"].realized_pnl_krw == 10000.0


def test_expectancy_with_mixed_results(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    # 3 wins of +1000, 2 losses of -500
    for i, sell_price in enumerate([71000, 71000, 71000, 69500, 69500]):
        _record_round_trip(
            ledger, symbol="A005930", qty=1, buy_price=70000, sell_price=sell_price,
            buy_strategy="x", order_id_prefix=f"r{i}"
        )
    s = aggregate_strategy_pnl(ledger)["x"]
    assert s.trades == 5
    assert s.wins == 3
    assert s.losses == 2
    assert s.win_rate == 0.6
    assert s.realized_pnl_krw == 3 * 1000 - 2 * 500
    assert s.expectancy_krw == s.realized_pnl_krw / 5
