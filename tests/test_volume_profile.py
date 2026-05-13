"""VolumeProfile + POC — 매물대 detector."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ks_ws.detectors.volume_profile import VolumeProfile, VolumeProfileTracker
from ks_ws.domain import Tick


def test_empty_profile() -> None:
    p = VolumeProfile()
    assert p.total_volume() == 0
    assert p.poc() is None
    assert p.value_area() is None


def test_feed_and_poc() -> None:
    p = VolumeProfile(bucket_size=100)
    p.feed(1010, 50)   # bucket 1000
    p.feed(1050, 100)  # bucket 1000
    p.feed(2010, 30)   # bucket 2000
    p.feed(2090, 20)   # bucket 2000
    assert p.poc() == 1000  # 150 > 50
    assert p.bucket_volume(1050) == 150


def test_top_n_buckets() -> None:
    p = VolumeProfile(bucket_size=100)
    p.feed(1010, 50)
    p.feed(2010, 200)
    p.feed(3010, 100)
    top = p.top_n_buckets(2)
    assert top == [(2000, 200), (3000, 100)]


def test_value_area_70pct() -> None:
    """3개 bucket: POC 70% / 좌우 15% — value_area = POC ± 1 bucket."""
    p = VolumeProfile(bucket_size=100)
    p.feed(1010, 15)  # bucket 1000
    p.feed(1110, 70)  # bucket 1100 = POC
    p.feed(1210, 15)  # bucket 1200
    va = p.value_area(coverage_pct=70.0)
    assert va is not None
    low, high = va
    # 70% target → POC bucket alone (70 vol) >= 70. low==high==POC.
    # 단 'while included < target' 라 = 70 < 70 false. 단 70 == target → loop exit.
    # implementations may include neighbor for slightly > coverage to be safe.
    assert low == 1100 and high == 1100


def test_value_area_expands_to_neighbors() -> None:
    """다양한 bucket. coverage 70 = POC + neighbors 합쳐서 70."""
    p = VolumeProfile(bucket_size=100)
    p.feed(1010, 20)   # 1000
    p.feed(1110, 50)   # 1100 = POC
    p.feed(1210, 30)   # 1200
    p.feed(1310, 10)   # 1300
    # total = 110. 70% = 77. POC alone 50 < 77 → expand.
    # 1200 (30) >= 1000 (20) → add 1200. 50+30=80 >= 77 → stop. Range 1100-1200.
    va = p.value_area(coverage_pct=70.0)
    assert va == (1100, 1200)


def test_value_area_invalid_coverage() -> None:
    p = VolumeProfile()
    p.feed(1000, 100)
    with pytest.raises(ValueError):
        p.value_area(coverage_pct=0)
    with pytest.raises(ValueError):
        p.value_area(coverage_pct=101)


def test_bucket_size_validation() -> None:
    with pytest.raises(ValueError):
        VolumeProfile(bucket_size=0)


def test_negative_input_rejected() -> None:
    p = VolumeProfile()
    with pytest.raises(ValueError):
        p.feed(-1, 100)
    with pytest.raises(ValueError):
        p.feed(100, -1)


def test_top_n_invalid() -> None:
    p = VolumeProfile()
    with pytest.raises(ValueError):
        p.top_n_buckets(0)


# --- VolumeProfileTracker (multi-symbol) ---


def _tick(sym: str, price: int, volume: int) -> Tick:
    return Tick(symbol=sym, price=price, volume=volume, timestamp=datetime.now(UTC))


def test_tracker_per_symbol() -> None:
    tr = VolumeProfileTracker(bucket_size=100)
    tr.feed_tick(_tick("005930", 28000, 1000))
    tr.feed_tick(_tick("005930", 28100, 500))
    tr.feed_tick(_tick("000660", 180000, 100))
    p = tr.get("005930")
    assert p is not None
    assert p.poc() == 28000
    assert tr.get("000660").bucket_volume(180000) == 100  # type: ignore[union-attr]
    assert set(tr.all_symbols()) == {"005930", "000660"}


def test_tracker_skip_zero_volume_snapshot() -> None:
    """WARM tier snapshot ticks have volume=0; tracker skips them."""
    tr = VolumeProfileTracker()
    tr.feed_tick(_tick("005930", 28000, 0))
    assert tr.get("005930") is None


def test_tracker_unknown_symbol() -> None:
    tr = VolumeProfileTracker()
    assert tr.get("unknown") is None
