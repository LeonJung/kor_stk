from datetime import UTC, datetime

from ks_ws.events import (
    Event,
    GapUp,
    OrderbookImbalance,
    ProgramFlowEnter,
    ProgramFlowExit,
    VolumeSpike,
)


def _now():
    return datetime.now(UTC)


def test_event_type_returns_subclass_name():
    e = ProgramFlowEnter(
        symbol="005930",
        timestamp=_now(),
        delta_krw=2_000_000_000,
        window_seconds=30,
    )
    assert e.event_type == "ProgramFlowEnter"


def test_subclass_is_instance_of_event():
    e = VolumeSpike(symbol="005930", timestamp=_now(), multiplier=3.5, window_seconds=60)
    assert isinstance(e, Event)


def test_dispatch_via_isinstance():
    events: list[Event] = [
        ProgramFlowEnter(symbol="A", timestamp=_now(), delta_krw=1, window_seconds=30),
        ProgramFlowExit(symbol="A", timestamp=_now(), delta_krw=-1, window_seconds=30),
        OrderbookImbalance(symbol="A", timestamp=_now(), bid_to_ask_ratio=2.0, levels_used=5),
        GapUp(symbol="A", timestamp=_now(), gap_pct=4.5),
    ]
    enters = [e for e in events if isinstance(e, ProgramFlowEnter)]
    flow_events = [e for e in events if isinstance(e, ProgramFlowEnter | ProgramFlowExit)]
    assert len(enters) == 1
    assert len(flow_events) == 2


def test_event_serialization_roundtrip():
    e = OrderbookImbalance(symbol="005930", timestamp=_now(), bid_to_ask_ratio=2.5, levels_used=10)
    raw = e.model_dump_json()
    e2 = OrderbookImbalance.model_validate_json(raw)
    assert e2 == e
