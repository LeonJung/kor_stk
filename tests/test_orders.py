from datetime import UTC, datetime

from ks_ws.domain import OrderIntent, Side
from ks_ws.orders import MockOrderRouter, SubmittedOrder


def _intent(symbol="005930", qty=50):
    return OrderIntent(
        symbol=symbol,
        side=Side.BUY,
        quantity=qty,
        timestamp=datetime.now(UTC),
    )


def test_submit_returns_submitted_order():
    router = MockOrderRouter()
    intent = _intent()
    out = router.submit(intent)
    assert isinstance(out, SubmittedOrder)
    assert out.intent is intent
    assert out.order_id == "mock-1"


def test_order_ids_are_unique_and_monotonic():
    router = MockOrderRouter()
    ids = [router.submit(_intent()).order_id for _ in range(5)]
    assert ids == [f"mock-{i}" for i in range(1, 6)]


def test_submitted_orders_are_recorded():
    router = MockOrderRouter()
    a = router.submit(_intent("005930"))
    b = router.submit(_intent("000660"))
    assert router.submitted == [a, b]


def test_submitted_returns_a_copy():
    router = MockOrderRouter()
    router.submit(_intent())
    snapshot = router.submitted
    snapshot.clear()
    assert len(router.submitted) == 1


def test_clear_resets_history_and_counter():
    router = MockOrderRouter()
    router.submit(_intent())
    router.submit(_intent())
    router.clear()
    assert router.submitted == []
    assert router.submit(_intent()).order_id == "mock-1"


def test_submitted_at_is_timezone_aware_utc():
    router = MockOrderRouter()
    out = router.submit(_intent())
    assert out.submitted_at.tzinfo is not None
    # within 5 seconds of now
    assert abs((datetime.now(UTC) - out.submitted_at).total_seconds()) < 5


def test_submittedorder_is_immutable():
    import dataclasses

    import pytest

    router = MockOrderRouter()
    out = router.submit(_intent())
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.order_id = "tampered"  # type: ignore[misc]
