from datetime import UTC, datetime

import pytest

from ks_ws.domain import Side
from ks_ws.events import GapUp, OrderbookImbalance, ProgramFlowEnter, VolumeSpike
from ks_ws.strategies.gap_up import GapUpStrategy
from ks_ws.strategies.orderbook_imbalance import OrderbookImbalanceStrategy
from ks_ws.strategies.volume_spike import VolumeSpikeStrategy


def _now():
    return datetime.now(UTC)


# VolumeSpikeStrategy ------------------------------------------------------


def test_volume_spike_strategy_emits_buy_on_spike():
    s = VolumeSpikeStrategy(confidence_cap_multiplier=5.0)
    event = VolumeSpike(symbol="005930", timestamp=_now(), multiplier=2.5, window_seconds=60)
    sigs = s.on_event(event)
    assert len(sigs) == 1
    assert sigs[0].side == Side.BUY
    assert sigs[0].symbol == "005930"
    assert sigs[0].strategy == "volume_spike"


def test_volume_spike_confidence_scales():
    s = VolumeSpikeStrategy(confidence_cap_multiplier=5.0, confidence_floor=0.0)
    weak = s.on_event(VolumeSpike(symbol="A", timestamp=_now(), multiplier=2.0, window_seconds=60))[
        0
    ]
    strong = s.on_event(
        VolumeSpike(symbol="A", timestamp=_now(), multiplier=4.0, window_seconds=60)
    )[0]
    assert weak.confidence == pytest.approx(0.4)
    assert strong.confidence == pytest.approx(0.8)


def test_volume_spike_caps_at_one():
    s = VolumeSpikeStrategy(confidence_cap_multiplier=2.0)
    sig = s.on_event(VolumeSpike(symbol="A", timestamp=_now(), multiplier=10, window_seconds=60))[0]
    assert sig.confidence == 1.0


def test_volume_spike_floor_applied():
    s = VolumeSpikeStrategy(confidence_cap_multiplier=10.0, confidence_floor=0.4)
    sig = s.on_event(VolumeSpike(symbol="A", timestamp=_now(), multiplier=2.0, window_seconds=60))[
        0
    ]
    assert sig.confidence == 0.4  # below 0.2 scaled, floor applies


def test_volume_spike_ignores_other_events():
    s = VolumeSpikeStrategy()
    assert s.on_event(GapUp(symbol="A", timestamp=_now(), gap_pct=4.0)) == []


def test_volume_spike_param_validation():
    with pytest.raises(ValueError):
        VolumeSpikeStrategy(confidence_cap_multiplier=1.0)
    with pytest.raises(ValueError):
        VolumeSpikeStrategy(confidence_floor=1.5)


# OrderbookImbalanceStrategy -----------------------------------------------


def test_orderbook_imbalance_strategy_emits_buy():
    s = OrderbookImbalanceStrategy(ratio_cap=4.0)
    e = OrderbookImbalance(symbol="005930", timestamp=_now(), bid_to_ask_ratio=2.0, levels_used=5)
    sigs = s.on_event(e)
    assert len(sigs) == 1
    assert sigs[0].side == Side.BUY
    assert sigs[0].confidence == pytest.approx(0.5)


def test_orderbook_imbalance_caps_at_one():
    s = OrderbookImbalanceStrategy(ratio_cap=2.0)
    sig = s.on_event(
        OrderbookImbalance(symbol="A", timestamp=_now(), bid_to_ask_ratio=10.0, levels_used=3)
    )[0]
    assert sig.confidence == 1.0


def test_orderbook_imbalance_floor_applied():
    s = OrderbookImbalanceStrategy(ratio_cap=10.0, confidence_floor=0.4)
    sig = s.on_event(
        OrderbookImbalance(symbol="A", timestamp=_now(), bid_to_ask_ratio=2.0, levels_used=3)
    )[0]
    assert sig.confidence == 0.4


def test_orderbook_imbalance_ignores_other_events():
    s = OrderbookImbalanceStrategy()
    assert (
        s.on_event(VolumeSpike(symbol="A", timestamp=_now(), multiplier=3, window_seconds=60)) == []
    )


def test_orderbook_imbalance_param_validation():
    with pytest.raises(ValueError):
        OrderbookImbalanceStrategy(ratio_cap=1.0)
    with pytest.raises(ValueError):
        OrderbookImbalanceStrategy(confidence_floor=2.0)


# GapUpStrategy ------------------------------------------------------------


def test_gap_up_strategy_emits_buy():
    s = GapUpStrategy(gap_pct_cap=10.0)
    sig = s.on_event(GapUp(symbol="005930", timestamp=_now(), gap_pct=4.0))[0]
    assert sig.side == Side.BUY
    assert sig.confidence == pytest.approx(0.4)


def test_gap_up_caps_at_one():
    s = GapUpStrategy(gap_pct_cap=5.0)
    sig = s.on_event(GapUp(symbol="A", timestamp=_now(), gap_pct=10.0))[0]
    assert sig.confidence == 1.0


def test_gap_up_floor_applied():
    s = GapUpStrategy(gap_pct_cap=20.0, confidence_floor=0.5)
    sig = s.on_event(GapUp(symbol="A", timestamp=_now(), gap_pct=3.0))[0]
    assert sig.confidence == 0.5


def test_gap_up_ignores_other_events():
    s = GapUpStrategy()
    assert (
        s.on_event(ProgramFlowEnter(symbol="A", timestamp=_now(), delta_krw=1, window_seconds=30))
        == []
    )


def test_gap_up_param_validation():
    with pytest.raises(ValueError):
        GapUpStrategy(gap_pct_cap=0)
    with pytest.raises(ValueError):
        GapUpStrategy(confidence_floor=-0.1)


# YAML loadability — sanity check the new strategies plug into the loader -


def test_reference_strategies_loadable_via_yaml():
    from ks_ws.strategies.config import load_portfolio_from_str

    yaml_text = """
strategies:
  - class: ks_ws.strategies.volume_spike.VolumeSpikeStrategy
    params:
      confidence_cap_multiplier: 4.0
  - class: ks_ws.strategies.orderbook_imbalance.OrderbookImbalanceStrategy
    params:
      ratio_cap: 3.0
  - class: ks_ws.strategies.gap_up.GapUpStrategy
    params:
      gap_pct_cap: 8.0
"""
    strategies, _ = load_portfolio_from_str(yaml_text)
    assert len(strategies) == 3
    assert strategies[0].confidence_cap_multiplier == 4.0
    assert strategies[1].ratio_cap == 3.0
    assert strategies[2].gap_pct_cap == 8.0
