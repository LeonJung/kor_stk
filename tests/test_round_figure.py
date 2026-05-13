"""RoundFigure detector — 책 사상 (호가단위 변경 경계 + 라운드 피겨) 식별."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ks_ws.bus import EventBus
from ks_ws.detectors.round_figure import (
    TICK_SIZE_BOUNDARIES_KRW,
    RoundFigureDetector,
    all_round_figures,
    decimal_round_figures,
    distance_bp,
    is_near_round_figure,
    nearest_round_figure,
    score_proximity,
)
from ks_ws.domain import Tick
from ks_ws.events import RoundFigureReached


def _tick(price: int, *, sym: str = "005930") -> Tick:
    return Tick(symbol=sym, price=price, volume=100, timestamp=datetime.now(UTC))


# --- helpers ---


def test_tick_boundaries_constant() -> None:
    assert TICK_SIZE_BOUNDARIES_KRW == (2_000, 5_000, 20_000, 50_000, 200_000, 500_000)


def test_decimal_round_figures_basic() -> None:
    res = decimal_round_figures(1_000, 100_000)
    # Expects 10, 20, 50, 100, 200, 500, 1k, 2k, 5k, 10k, 20k, 50k, 100k
    # but only those within range [1k, 100k]
    assert 10_000 in res
    assert 50_000 in res
    assert 100_000 in res
    assert 2_000 in res
    assert 5_000 in res
    # outside range
    assert 100 not in res
    assert 200_000 not in res


def test_all_round_figures_includes_tick_boundaries() -> None:
    res = all_round_figures(1_000, 600_000)
    for b in (2_000, 5_000, 20_000, 50_000, 200_000, 500_000):
        assert b in res


def test_nearest_round_figure_exact_match() -> None:
    assert nearest_round_figure(50_000) == 50_000
    assert nearest_round_figure(100_000) == 100_000
    assert nearest_round_figure(2_000) == 2_000


def test_nearest_round_figure_close() -> None:
    # 49_950 → 50_000 (가장 가까움)
    assert nearest_round_figure(49_950) == 50_000
    # 99_800 → 100_000
    assert nearest_round_figure(99_800) == 100_000


def test_nearest_round_figure_high_price() -> None:
    """삼성전자 ~28만원 부근."""
    res = nearest_round_figure(280_000)
    # 가장 가까움 = 200_000 또는 500_000 (tick boundary), 300_000 등 decimal
    # 200k 와 300k 둘 다 candidate. 300k 가 가까움 (20k 차이 < 80k 차이).
    assert res in (300_000, 200_000)


def test_distance_bp() -> None:
    # 50_100 - 50_000 = 100, /50_000 *10000 = 20bp
    assert distance_bp(50_100, 50_000) == pytest.approx(20.0)
    # 49_900 → -20bp
    assert distance_bp(49_900, 50_000) == pytest.approx(-20.0)


def test_distance_bp_zero_target() -> None:
    assert distance_bp(100, 0) == 0.0


def test_is_near_round_figure() -> None:
    # 49_950 (target 50_000) → 10bp away → within ±20bp default tolerance
    assert is_near_round_figure(49_950)
    # 49_500 (target 50_000) → 100bp = 1% away → outside
    assert not is_near_round_figure(49_500)
    # exact match
    assert is_near_round_figure(50_000)


def test_score_proximity_exact() -> None:
    assert score_proximity(50_000) == 1.3


def test_score_proximity_at_tolerance() -> None:
    # 49_900 (20bp from 50k) at exact tolerance → score 1.0
    assert score_proximity(49_900) == pytest.approx(1.0)


def test_score_proximity_far() -> None:
    # 49_500 (100bp from 50k) outside default tolerance → 0.9 penalty
    assert score_proximity(49_500) == 0.9


def test_score_proximity_invalid() -> None:
    assert score_proximity(0) == 1.0
    assert score_proximity(-100) == 1.0


# --- RoundFigureDetector ---


def test_detector_emits_on_cross() -> None:
    bus = EventBus()
    sub = bus.subscribe(RoundFigureReached)
    det = RoundFigureDetector(bus)

    # Start far from round figure
    det.feed(_tick(48_000))
    # Move near 50k (= within tolerance)
    det.feed(_tick(49_990))

    events: list[RoundFigureReached] = []
    while sub.qsize() > 0:
        events.append(sub.get_nowait())
    assert len(events) == 1
    assert events[0].boundary_price == 50_000
    assert events[0].actual_price == 49_990


def test_detector_hysteresis_no_rechatter() -> None:
    """Hysteresis: cross → emit. Stay → no re-emit. Same target oscillation → no re-emit."""
    bus = EventBus()
    sub = bus.subscribe(RoundFigureReached)
    det = RoundFigureDetector(bus)

    det.feed(_tick(48_000))  # far
    det.feed(_tick(49_990))  # enter — emit
    det.feed(_tick(50_001))  # still near 50k — no emit
    det.feed(_tick(49_995))  # still near 50k — no emit

    events = []
    while sub.qsize() > 0:
        events.append(sub.get_nowait())
    assert len(events) == 1


def test_detector_emits_again_after_leaving_and_new_target() -> None:
    bus = EventBus()
    sub = bus.subscribe(RoundFigureReached)
    det = RoundFigureDetector(bus)

    det.feed(_tick(48_000))  # far
    det.feed(_tick(49_990))  # near 50k — emit
    det.feed(_tick(70_000))  # far again
    det.feed(_tick(99_980))  # near 100k — emit (new target)

    events = []
    while sub.qsize() > 0:
        events.append(sub.get_nowait())
    assert len(events) == 2
    assert events[0].boundary_price == 50_000
    assert events[1].boundary_price == 100_000


def test_detector_invalid_tolerance() -> None:
    bus = EventBus()
    with pytest.raises(ValueError):
        RoundFigureDetector(bus, tolerance_bp=-1)
