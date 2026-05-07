from datetime import UTC, datetime

import pytest

from ks_ws.domain import OrderIntent, Side
from ks_ws.risk import Risk


def _intent(side=Side.BUY, qty=50, symbol="005930"):
    return OrderIntent(
        symbol=symbol,
        side=side,
        quantity=qty,
        timestamp=datetime.now(UTC),
    )


def test_buy_within_cap_passes_through():
    r = Risk(max_position_per_symbol=100)
    out = r.check(_intent(qty=50), current_position=0)
    assert out is not None
    assert out.quantity == 50


def test_buy_reduced_to_remaining_cap():
    r = Risk(max_position_per_symbol=100)
    out = r.check(_intent(qty=80), current_position=70)
    assert out is not None
    assert out.quantity == 30  # 100 - 70


def test_buy_rejected_when_at_cap():
    r = Risk(max_position_per_symbol=100)
    assert r.check(_intent(qty=10), current_position=100) is None


def test_buy_rejected_when_over_cap_already():
    """Defensive: if external state shows over-cap (e.g. from broker),
    further buys block."""
    r = Risk(max_position_per_symbol=100)
    assert r.check(_intent(qty=10), current_position=150) is None


def test_sell_is_not_position_capped():
    r = Risk(max_position_per_symbol=100)
    out = r.check(_intent(side=Side.SELL, qty=200), current_position=0)
    assert out is not None
    assert out.quantity == 200  # unchanged


def test_daily_loss_circuit_blocks_all_new_orders():
    r = Risk(daily_loss_limit_krw=5_000_000)
    assert r.check(_intent(side=Side.BUY), realized_pnl_today_krw=-5_000_000) is None
    assert r.check(_intent(side=Side.SELL), realized_pnl_today_krw=-5_000_000) is None
    assert r.check(_intent(side=Side.BUY), realized_pnl_today_krw=-10_000_000) is None


def test_daily_loss_above_limit_does_not_block():
    r = Risk(daily_loss_limit_krw=5_000_000)
    out = r.check(_intent(), realized_pnl_today_krw=-4_999_999)
    assert out is not None


def test_daily_loss_disabled_when_none():
    r = Risk(daily_loss_limit_krw=None)
    out = r.check(_intent(), realized_pnl_today_krw=-1_000_000_000)
    assert out is not None


def test_invalid_max_position_rejected():
    with pytest.raises(ValueError):
        Risk(max_position_per_symbol=0)


def test_invalid_loss_limit_rejected():
    with pytest.raises(ValueError):
        Risk(daily_loss_limit_krw=0)


def test_check_does_not_mutate_input_intent():
    r = Risk(max_position_per_symbol=100)
    intent = _intent(qty=80)
    r.check(intent, current_position=70)
    assert intent.quantity == 80  # original untouched


def test_returned_intent_preserves_other_fields():
    r = Risk(max_position_per_symbol=100)
    intent = OrderIntent(
        symbol="005930",
        side=Side.BUY,
        quantity=80,
        order_type="limit",
        limit_price=70_000,
        timestamp=datetime.now(UTC),
        sources=("alpha", "beta"),
    )
    out = r.check(intent, current_position=70)
    assert out is not None
    assert out.quantity == 30
    assert out.order_type == "limit"
    assert out.limit_price == 70_000
    assert out.sources == ("alpha", "beta")
