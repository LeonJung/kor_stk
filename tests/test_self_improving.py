"""Tests for SelfImprovingWeightUpdater."""

from datetime import UTC, datetime

import pytest

from ks_ws.domain import OrderIntent, Side
from ks_ws.orders import SubmittedOrder
from ks_ws.storage.ledger import Ledger
from ks_ws.strategies.allocator import Allocator
from ks_ws.strategies.self_improving import SelfImprovingWeightUpdater


def _record(ledger: Ledger, *, strategy: str, qty: int, buy: int, sell: int, prefix: str = "r"):
    now = datetime.now(UTC)
    buy_intent = OrderIntent(
        symbol="X", side=Side.BUY, quantity=qty, timestamp=now, sources=(strategy,)
    )
    b = SubmittedOrder(order_id=f"{prefix}-buy", intent=buy_intent, submitted_at=now)
    ledger.record_order(b)
    ledger.apply_fill(order_id=b.order_id, symbol="X", side=Side.BUY, quantity=qty, price=buy)
    sell_intent = OrderIntent(
        symbol="X", side=Side.SELL, quantity=qty, timestamp=now, sources=(strategy,)
    )
    s = SubmittedOrder(order_id=f"{prefix}-sell", intent=sell_intent, submitted_at=now)
    ledger.record_order(s)
    ledger.apply_fill(order_id=s.order_id, symbol="X", side=Side.SELL, quantity=qty, price=sell)


# Validation ---------------------------------------------------------------


def test_validation(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    with pytest.raises(ValueError):
        SelfImprovingWeightUpdater(ledger=ledger, learning_rate=0)
    with pytest.raises(ValueError):
        SelfImprovingWeightUpdater(ledger=ledger, learning_rate=2)
    with pytest.raises(ValueError):
        SelfImprovingWeightUpdater(ledger=ledger, weight_floor=-0.1)
    with pytest.raises(ValueError):
        SelfImprovingWeightUpdater(ledger=ledger, k_smooth=0)
    with pytest.raises(ValueError):
        SelfImprovingWeightUpdater(ledger=ledger, normalize_by=0)


# Behavior -----------------------------------------------------------------


def test_winning_strategy_weight_increases(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    # 30 trades of +5_000 = total +150,000, expectancy = +5_000
    for i in range(30):
        _record(ledger, strategy="winner", qty=10, buy=10000, sell=10500, prefix=f"w{i}")
    allocator = Allocator()
    allocator.set_weight("winner", 1.0)
    updater = SelfImprovingWeightUpdater(ledger=ledger, learning_rate=0.5, normalize_by=10_000)
    report = updater.update(allocator)
    new = allocator.weight_for("winner")
    assert new > 1.0
    assert any(c.strategy == "winner" and c.new_weight > c.old_weight for c in report.changes)


def test_losing_strategy_weight_decreases(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    for i in range(30):
        _record(ledger, strategy="loser", qty=10, buy=10000, sell=9500, prefix=f"l{i}")
    allocator = Allocator()
    allocator.set_weight("loser", 1.0)
    updater = SelfImprovingWeightUpdater(ledger=ledger, learning_rate=0.5, normalize_by=10_000)
    updater.update(allocator)
    assert allocator.weight_for("loser") < 1.0


def test_few_trades_low_stability_small_change(tmp_path):
    """k_smooth dampens changes for low-trade strategies."""
    ledger = Ledger(tmp_path / "l.sqlite")
    _record(ledger, strategy="newbie", qty=10, buy=10000, sell=11000, prefix="n0")
    allocator = Allocator()
    allocator.set_weight("newbie", 1.0)
    updater = SelfImprovingWeightUpdater(
        ledger=ledger, learning_rate=0.5, normalize_by=10_000, k_smooth=10.0
    )
    updater.update(allocator)
    # 1 trade only → stability = 1/11 ≈ 0.09; small change
    new = allocator.weight_for("newbie")
    assert 1.0 < new < 1.1


def test_weight_floor_clamps(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    for i in range(50):
        _record(ledger, strategy="terrible", qty=100, buy=10000, sell=5000, prefix=f"t{i}")
    allocator = Allocator()
    allocator.set_weight("terrible", 0.05)
    updater = SelfImprovingWeightUpdater(
        ledger=ledger, learning_rate=1.0, normalize_by=10_000, weight_floor=0.0
    )
    updater.update(allocator)
    assert allocator.weight_for("terrible") == 0.0  # clamped


def test_weight_cap_clamps(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    for i in range(50):
        _record(ledger, strategy="superstar", qty=100, buy=10000, sell=15000, prefix=f"s{i}")
    allocator = Allocator()
    allocator.set_weight("superstar", 1.5)
    updater = SelfImprovingWeightUpdater(
        ledger=ledger, learning_rate=1.0, normalize_by=10_000, weight_cap=2.0
    )
    updater.update(allocator)
    assert allocator.weight_for("superstar") == 2.0  # clamped


def test_no_change_for_zero_expectancy(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    # equal wins/losses around break-even
    for i in range(10):
        _record(ledger, strategy="break_even", qty=10, buy=10000, sell=10100, prefix=f"a{i}")
    for i in range(10):
        _record(ledger, strategy="break_even", qty=10, buy=10000, sell=9900, prefix=f"b{i}")
    allocator = Allocator()
    allocator.set_weight("break_even", 1.0)
    updater = SelfImprovingWeightUpdater(ledger=ledger, learning_rate=0.5, normalize_by=10_000)
    updater.update(allocator)
    assert allocator.weight_for("break_even") == pytest.approx(1.0, abs=0.01)


def test_report_summary_contains_changes(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    _record(ledger, strategy="x", qty=10, buy=10000, sell=11000, prefix="x0")
    allocator = Allocator()
    updater = SelfImprovingWeightUpdater(ledger=ledger)
    report = updater.update(allocator)
    text = report.summary()
    assert "x" in text
    assert "→" in text or "=" in text


def test_unknown_strategy_with_default_weight(tmp_path):
    """Strategy never explicitly weighted — default 1.0; updater can change it."""
    ledger = Ledger(tmp_path / "l.sqlite")
    for i in range(30):
        _record(ledger, strategy="implicit", qty=10, buy=10000, sell=10500, prefix=f"i{i}")
    allocator = Allocator()  # no set_weight call
    updater = SelfImprovingWeightUpdater(ledger=ledger, learning_rate=0.5, normalize_by=10_000)
    updater.update(allocator)
    assert allocator.weight_for("implicit") > 1.0
