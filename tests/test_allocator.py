from datetime import UTC, datetime

import pytest

from ks_ws.domain import Side, Signal
from ks_ws.strategies.allocator import Allocator


def _signal(strategy="s1", side=Side.BUY, confidence=0.5, symbol="005930"):
    return Signal(
        symbol=symbol,
        side=side,
        confidence=confidence,
        strategy=strategy,
        timestamp=datetime.now(UTC),
    )


def test_empty_signals_yields_empty_intents():
    a = Allocator()
    assert a.combine([]) == []


def test_single_buy_signal_produces_buy_intent():
    a = Allocator(max_position_per_symbol=100)
    intents = a.combine([_signal(confidence=0.5)])
    assert len(intents) == 1
    assert intents[0].side == Side.BUY
    assert intents[0].quantity == 50  # 0.5 * 100


def test_full_confidence_uses_max_position():
    a = Allocator(max_position_per_symbol=100)
    intents = a.combine([_signal(confidence=1.0)])
    assert intents[0].quantity == 100


def test_buy_and_sell_with_equal_weight_cancel():
    a = Allocator()
    sigs = [
        _signal(strategy="s1", side=Side.BUY, confidence=0.7),
        _signal(strategy="s2", side=Side.SELL, confidence=0.7),
    ]
    assert a.combine(sigs) == []


def test_buy_dominates_when_stronger():
    a = Allocator(max_position_per_symbol=100)
    sigs = [
        _signal(strategy="s1", side=Side.BUY, confidence=0.8),
        _signal(strategy="s2", side=Side.SELL, confidence=0.3),
    ]
    intents = a.combine(sigs)
    assert len(intents) == 1
    assert intents[0].side == Side.BUY
    # net = 0.8 - 0.3 = 0.5 -> 50 shares
    assert intents[0].quantity == 50


def test_weight_amplifies_strategy_influence():
    a = Allocator(max_position_per_symbol=100)
    a.set_weight("strong", 2.0)
    sigs = [
        _signal(strategy="strong", side=Side.BUY, confidence=0.5),
        _signal(strategy="weak", side=Side.SELL, confidence=0.6),
    ]
    intents = a.combine(sigs)
    # net = 2.0*0.5 - 1.0*0.6 = 0.4 -> 40
    assert intents[0].side == Side.BUY
    assert intents[0].quantity == 40


def test_clamps_to_max_when_compounded_signals_exceed_one():
    a = Allocator(max_position_per_symbol=100)
    sigs = [
        _signal(strategy="s1", side=Side.BUY, confidence=0.8),
        _signal(strategy="s2", side=Side.BUY, confidence=0.8),
    ]
    intents = a.combine(sigs)
    # net = 1.6, clamped to 1.0 -> 100
    assert intents[0].quantity == 100


def test_separate_symbols_produce_separate_intents():
    a = Allocator()
    sigs = [
        _signal(symbol="005930", side=Side.BUY, confidence=0.5),
        _signal(symbol="000660", side=Side.SELL, confidence=0.5),
    ]
    intents = a.combine(sigs)
    assert len(intents) == 2
    by_symbol = {i.symbol: i for i in intents}
    assert by_symbol["005930"].side == Side.BUY
    assert by_symbol["000660"].side == Side.SELL


def test_sources_record_contributing_strategies():
    a = Allocator()
    sigs = [
        _signal(strategy="alpha", side=Side.BUY, confidence=0.5),
        _signal(strategy="beta", side=Side.BUY, confidence=0.5),
    ]
    intent = a.combine(sigs)[0]
    assert set(intent.sources) == {"alpha", "beta"}


def test_unknown_strategy_uses_default_weight_one():
    a = Allocator()
    assert a.weight_for("never_set") == 1.0


def test_negative_weight_rejected():
    a = Allocator()
    with pytest.raises(ValueError):
        a.set_weight("bad", -0.5)


def test_invalid_max_position_rejected():
    with pytest.raises(ValueError):
        Allocator(max_position_per_symbol=0)


def test_minimum_quantity_is_one():
    """Tiny non-zero net should still yield at least one share."""
    a = Allocator(max_position_per_symbol=100)
    intents = a.combine([_signal(confidence=0.001)])
    assert intents[0].quantity == 1
