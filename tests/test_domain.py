from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ks_ws.domain import Bar, OrderBook, OrderBookLevel, OrderIntent, Side, Signal, Tick


def _now():
    return datetime.now(UTC)


def _bar(**overrides):
    base = dict(
        symbol="005930",
        timestamp=_now(),
        timeframe="1d",
        open=70_000,
        high=71_000,
        low=69_500,
        close=70_500,
        volume=10_000_000,
        value=703_000_000_000,
    )
    return Bar(**(base | overrides))


def test_bar_constructs_and_reads():
    b = _bar()
    assert b.close == 70_500
    assert b.value == 703_000_000_000


def test_bar_is_frozen():
    b = _bar()
    with pytest.raises(ValidationError):
        b.close = 99_999


def test_signal_confidence_must_be_in_unit_range():
    kw = dict(symbol="005930", side=Side.BUY, strategy="x", timestamp=_now())
    Signal(**kw, confidence=0.0)
    Signal(**kw, confidence=1.0)
    with pytest.raises(ValidationError):
        Signal(**kw, confidence=1.5)
    with pytest.raises(ValidationError):
        Signal(**kw, confidence=-0.1)


def test_signal_defaults():
    s = Signal(
        symbol="005930",
        side=Side.BUY,
        confidence=0.7,
        strategy="prog_flow",
        timestamp=_now(),
    )
    assert s.urgency == "normal"
    assert s.note == ""


def test_orderbook_holds_levels():
    ob = OrderBook(
        symbol="005930",
        timestamp=_now(),
        bids=(OrderBookLevel(price=70_000, volume=100),),
        asks=(OrderBookLevel(price=70_100, volume=200),),
    )
    assert ob.bids[0].price == 70_000
    assert ob.asks[0].volume == 200


def test_orderintent_defaults_to_market():
    o = OrderIntent(
        symbol="005930",
        side=Side.BUY,
        quantity=10,
        timestamp=_now(),
    )
    assert o.order_type == "market"
    assert o.limit_price is None
    assert o.sources == ()


def test_tick_aggressor_optional():
    t = Tick(symbol="005930", timestamp=_now(), price=70_000, volume=10)
    assert t.aggressor is None


def test_side_serializes_as_string():
    s = Signal(
        symbol="005930",
        side=Side.SELL,
        confidence=0.5,
        strategy="x",
        timestamp=_now(),
    )
    dumped = s.model_dump()
    assert dumped["side"] == "sell"


def test_bar_serialization_roundtrip():
    b = _bar()
    raw = b.model_dump_json()
    b2 = Bar.model_validate_json(raw)
    assert b2 == b
