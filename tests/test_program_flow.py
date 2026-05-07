from datetime import UTC, datetime

import pytest

from ks_ws.bus import EventBus
from ks_ws.detectors.program_flow import ProgramFlowDetector
from ks_ws.domain import Side
from ks_ws.events import GapUp, ProgramFlowEnter, ProgramFlowExit
from ks_ws.strategies.program_flow import ProgramFlowStrategy


def _now():
    return datetime.now(UTC)


# --- Detector -------------------------------------------------------------


def test_detector_emits_enter_on_threshold_cross():
    bus = EventBus()
    sub = bus.subscribe(ProgramFlowEnter)
    d = ProgramFlowDetector(bus, entry_threshold_krw=1_000_000_000, exit_threshold_krw=100_000_000)
    d.feed("005930", 1_500_000_000, _now())
    assert sub.qsize() == 1
    e = sub.get_nowait()
    assert e.symbol == "005930"
    assert e.delta_krw == 1_500_000_000


def test_detector_does_not_re_emit_enter_while_active():
    bus = EventBus()
    sub = bus.subscribe(ProgramFlowEnter)
    d = ProgramFlowDetector(bus)
    d.feed("005930", 2_000_000_000, _now())
    d.feed("005930", 2_500_000_000, _now())  # still well above entry
    assert sub.qsize() == 1


def test_detector_emits_exit_after_drop():
    bus = EventBus()
    enter_sub = bus.subscribe(ProgramFlowEnter)
    exit_sub = bus.subscribe(ProgramFlowExit)
    d = ProgramFlowDetector(bus)
    d.feed("005930", 2_000_000_000, _now())  # enter
    d.feed("005930", 50_000_000, _now())  # exit
    assert enter_sub.qsize() == 1
    assert exit_sub.qsize() == 1


def test_detector_hysteresis_prevents_chatter():
    """Below entry but above exit should not toggle state."""
    bus = EventBus()
    enter_sub = bus.subscribe(ProgramFlowEnter)
    exit_sub = bus.subscribe(ProgramFlowExit)
    d = ProgramFlowDetector(bus, entry_threshold_krw=1_000_000_000, exit_threshold_krw=100_000_000)
    # Drift in the dead zone — no events
    d.feed("005930", 500_000_000, _now())
    d.feed("005930", 800_000_000, _now())
    d.feed("005930", 300_000_000, _now())
    assert enter_sub.qsize() == 0
    assert exit_sub.qsize() == 0


def test_detector_state_per_symbol():
    bus = EventBus()
    sub = bus.subscribe(ProgramFlowEnter)
    d = ProgramFlowDetector(bus)
    d.feed("005930", 2_000_000_000, _now())
    d.feed("000660", 2_000_000_000, _now())
    assert sub.qsize() == 2
    assert d.is_entered("005930") is True
    assert d.is_entered("000660") is True


def test_detector_validates_threshold_ordering():
    bus = EventBus()
    with pytest.raises(ValueError):
        ProgramFlowDetector(bus, entry_threshold_krw=100, exit_threshold_krw=200)


def test_detector_propagates_window_seconds():
    bus = EventBus()
    sub = bus.subscribe(ProgramFlowEnter)
    d = ProgramFlowDetector(bus, window_seconds=60)
    d.feed("005930", 2_000_000_000, _now())
    assert sub.get_nowait().window_seconds == 60


# --- Strategy -------------------------------------------------------------


def test_strategy_emits_buy_on_enter():
    s = ProgramFlowStrategy()
    e = ProgramFlowEnter(
        symbol="005930", timestamp=_now(), delta_krw=2_500_000_000, window_seconds=30
    )
    sigs = s.on_event(e)
    assert len(sigs) == 1
    assert sigs[0].side == Side.BUY
    assert sigs[0].symbol == "005930"
    assert sigs[0].strategy == "program_flow"


def test_strategy_buy_confidence_scales_with_delta():
    s = ProgramFlowStrategy(confidence_cap_krw=5_000_000_000)
    weak = s.on_event(
        ProgramFlowEnter(symbol="A", timestamp=_now(), delta_krw=1_000_000_000, window_seconds=30)
    )[0]
    strong = s.on_event(
        ProgramFlowEnter(symbol="A", timestamp=_now(), delta_krw=4_000_000_000, window_seconds=30)
    )[0]
    assert weak.confidence < strong.confidence
    assert weak.confidence == pytest.approx(0.2)
    assert strong.confidence == pytest.approx(0.8)


def test_strategy_buy_confidence_caps_at_one():
    s = ProgramFlowStrategy(confidence_cap_krw=1_000_000_000)
    sig = s.on_event(
        ProgramFlowEnter(symbol="A", timestamp=_now(), delta_krw=10_000_000_000, window_seconds=30)
    )[0]
    assert sig.confidence == 1.0


def test_strategy_emits_sell_on_exit():
    s = ProgramFlowStrategy(exit_confidence=0.7)
    sig = s.on_event(
        ProgramFlowExit(symbol="A", timestamp=_now(), delta_krw=20_000_000, window_seconds=30)
    )[0]
    assert sig.side == Side.SELL
    assert sig.confidence == 0.7


def test_strategy_ignores_unrelated_events():
    s = ProgramFlowStrategy()
    sigs = s.on_event(GapUp(symbol="A", timestamp=_now(), gap_pct=4.0))
    assert sigs == []


def test_strategy_validates_params():
    with pytest.raises(ValueError):
        ProgramFlowStrategy(confidence_cap_krw=0)
    with pytest.raises(ValueError):
        ProgramFlowStrategy(exit_confidence=1.5)


# --- End-to-end through bus -----------------------------------------------


def test_detector_to_strategy_via_bus():
    """Wire the detector and strategy through the bus exactly as
    production would: detector publishes events, the runtime hands them
    to the strategy by isinstance, signals come out the other side."""
    bus = EventBus()
    sub = bus.subscribe(ProgramFlowEnter)
    detector = ProgramFlowDetector(bus)
    strategy = ProgramFlowStrategy()

    detector.feed("005930", 3_000_000_000, _now())
    event = sub.get_nowait()
    signals = strategy.on_event(event)

    assert len(signals) == 1
    assert signals[0].side == Side.BUY
    assert signals[0].symbol == "005930"
