"""SectorRotation tests."""
from __future__ import annotations

import pytest

from ks_ws.bus import EventBus
from ks_ws.detectors.sector_rotation import (
    SectorStrengthTracker,
    compute_sector_strength,
    rank_sectors,
)
from ks_ws.events import SectorRotation
from ks_ws.sources.sector import SectorClassifier


def test_compute_sector_strength_basic() -> None:
    clf = SectorClassifier()
    returns = {
        "005930": 2.0,   # IT
        "000660": 4.0,   # IT
        "005380": -1.0,  # Consumer Discretionary
        "000270": 1.0,   # Consumer Discretionary
        "207940": 5.0,   # Health Care
    }
    res = compute_sector_strength(returns, clf)
    assert res["Information Technology"] == pytest.approx(3.0)  # (2+4)/2
    assert res["Consumer Discretionary"] == pytest.approx(0.0)  # (-1+1)/2
    assert res["Health Care"] == 5.0


def test_compute_strength_skips_unknown() -> None:
    clf = SectorClassifier()
    returns = {"005930": 2.0, "999999": 10.0}  # 999999 unknown
    res = compute_sector_strength(returns, clf)
    assert "Information Technology" in res
    assert all(sec != "unknown" for sec in res)


def test_compute_strength_empty() -> None:
    clf = SectorClassifier()
    assert compute_sector_strength({}, clf) == {}


def test_rank_sectors_desc() -> None:
    ranked = rank_sectors({"A": 2.0, "B": 5.0, "C": 1.0})
    assert ranked == [("B", 5.0), ("A", 2.0), ("C", 1.0)]


# --- SectorStrengthTracker ---


def test_tracker_first_snapshot_no_emit() -> None:
    bus = EventBus()
    sub = bus.subscribe(SectorRotation)
    tracker = SectorStrengthTracker(bus, SectorClassifier())
    tracker.snapshot({"005930": 3.0, "005380": 1.0})
    events = []
    while sub.qsize() > 0:
        events.append(sub.get_nowait())
    assert events == []  # 첫 snapshot — 비교 대상 없음


def test_tracker_rotation_emit_when_leader_changes() -> None:
    bus = EventBus()
    sub = bus.subscribe(SectorRotation)
    tracker = SectorStrengthTracker(bus, SectorClassifier())

    # Day 1: IT leads (005930 +3, 000660 +2 → IT 2.5), Consumer (005380 +0.5)
    tracker.snapshot({"005930": 3.0, "000660": 2.0, "005380": 0.5})

    # Day 2: Consumer leads (005380 +4 → CD 4.0), IT weak (005930 +0.5)
    tracker.snapshot({"005930": 0.5, "000660": 0.3, "005380": 4.0})

    events = []
    while sub.qsize() > 0:
        events.append(sub.get_nowait())
    assert len(events) == 1
    assert events[0].leading_sector == "Consumer Discretionary"
    assert events[0].leading_strength == 4.0


def test_tracker_no_emit_when_leader_unchanged() -> None:
    bus = EventBus()
    sub = bus.subscribe(SectorRotation)
    tracker = SectorStrengthTracker(bus, SectorClassifier())
    tracker.snapshot({"005930": 3.0, "005380": 1.0})
    tracker.snapshot({"005930": 2.5, "005380": 0.8})  # IT still leads
    events = []
    while sub.qsize() > 0:
        events.append(sub.get_nowait())
    assert events == []


def test_tracker_min_strength_diff_threshold() -> None:
    """leader 바뀌어도 strength 차이가 min_diff 미만이면 emit X."""
    bus = EventBus()
    sub = bus.subscribe(SectorRotation)
    tracker = SectorStrengthTracker(bus, SectorClassifier(), min_strength_diff_pct=1.0)
    # Day 1: IT 2.0 / CD 1.5 (IT leads). Day 2: IT 1.5 / CD 1.8 (CD leads,
    # but diff vs IT-now = 0.3 < 1.0 → no emit, weak rotation).
    tracker.snapshot({"005930": 2.0, "005380": 1.5})
    tracker.snapshot({"005930": 1.5, "005380": 1.8})
    events = []
    while sub.qsize() > 0:
        events.append(sub.get_nowait())
    assert events == []


def test_tracker_latest_and_history() -> None:
    tracker = SectorStrengthTracker(None, SectorClassifier())
    assert tracker.latest() is None
    tracker.snapshot({"005930": 2.0})
    assert tracker.latest() is not None
    assert tracker.history_len() == 1
    tracker.snapshot({"005930": 3.0})
    assert tracker.history_len() == 2


def test_tracker_history_capped() -> None:
    tracker = SectorStrengthTracker(None, SectorClassifier())
    for i in range(70):
        tracker.snapshot({"005930": float(i)})
    assert tracker.history_len() == 60  # capped


def test_tracker_no_bus_works_for_strength_only() -> None:
    """bus=None 일 때 snapshot 호출 가능, event 발행만 skip."""
    tracker = SectorStrengthTracker(None, SectorClassifier())
    tracker.snapshot({"005930": 2.0})
    tracker.snapshot({"005380": 4.0})  # leader change
    # No exception
    assert tracker.emit_count == 0
