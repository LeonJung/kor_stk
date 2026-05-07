from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.domain import Bar
from ks_ws.storage.bars import BarStore


def _bar(symbol: str, ts: datetime, close: int, *, timeframe: str = "1m") -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=ts,
        timeframe=timeframe,
        open=close,
        high=close + 50,
        low=close - 50,
        close=close,
        volume=1_000,
        value=close * 1_000,
    )


def _seq(symbol: str, start: datetime, count: int, base_close: int = 70_000) -> list[Bar]:
    return [_bar(symbol, start + timedelta(minutes=i), base_close + i) for i in range(count)]


def test_empty_write_is_noop(tmp_path):
    store = BarStore(tmp_path)
    assert store.write([]) == 0
    assert not (tmp_path / "bars").exists()


def test_single_bar_round_trip(tmp_path):
    store = BarStore(tmp_path)
    ts = datetime(2026, 5, 8, 9, 0, tzinfo=UTC)
    b = _bar("005930", ts, 70_000)
    assert store.write([b]) == 1

    expected = tmp_path / "bars" / "1m" / "005930" / "2026.parquet"
    assert expected.exists()

    [back] = list(store.read("005930", "1m"))
    assert back == b


def test_write_groups_by_symbol_year_timeframe(tmp_path):
    store = BarStore(tmp_path)
    bars = [
        _bar("005930", datetime(2026, 1, 1, tzinfo=UTC), 70_000, timeframe="1m"),
        _bar("005930", datetime(2026, 1, 2, tzinfo=UTC), 70_500, timeframe="1d"),
        _bar("005930", datetime(2025, 12, 31, tzinfo=UTC), 69_000, timeframe="1m"),
        _bar("000660", datetime(2026, 1, 1, tzinfo=UTC), 120_000, timeframe="1m"),
    ]
    assert store.write(bars) == 4
    assert (tmp_path / "bars" / "1m" / "005930" / "2026.parquet").exists()
    assert (tmp_path / "bars" / "1m" / "005930" / "2025.parquet").exists()
    assert (tmp_path / "bars" / "1d" / "005930" / "2026.parquet").exists()
    assert (tmp_path / "bars" / "1m" / "000660" / "2026.parquet").exists()


def test_read_returns_in_timestamp_order(tmp_path):
    store = BarStore(tmp_path)
    start = datetime(2026, 5, 8, 9, 0, tzinfo=UTC)
    bars = _seq("005930", start, 5)
    # write in shuffled order
    store.write([bars[3], bars[0], bars[4], bars[1], bars[2]])

    out = list(store.read("005930", "1m"))
    assert out == bars


def test_read_filters_by_start(tmp_path):
    store = BarStore(tmp_path)
    start = datetime(2026, 5, 8, 9, 0, tzinfo=UTC)
    bars = _seq("005930", start, 5)
    store.write(bars)

    out = list(store.read("005930", "1m", start=bars[2].timestamp))
    assert out == bars[2:]


def test_read_filters_by_end_exclusive(tmp_path):
    store = BarStore(tmp_path)
    start = datetime(2026, 5, 8, 9, 0, tzinfo=UTC)
    bars = _seq("005930", start, 5)
    store.write(bars)

    out = list(store.read("005930", "1m", end=bars[2].timestamp))
    assert out == bars[:2]


def test_read_filters_by_range(tmp_path):
    store = BarStore(tmp_path)
    start = datetime(2026, 5, 8, 9, 0, tzinfo=UTC)
    bars = _seq("005930", start, 6)
    store.write(bars)

    out = list(store.read("005930", "1m", start=bars[1].timestamp, end=bars[4].timestamp))
    assert out == bars[1:4]


def test_read_unknown_symbol_yields_empty(tmp_path):
    store = BarStore(tmp_path)
    assert list(store.read("MISSING", "1m")) == []


def test_append_merges_into_existing_file(tmp_path):
    store = BarStore(tmp_path)
    start = datetime(2026, 5, 8, 9, 0, tzinfo=UTC)
    first = _seq("005930", start, 3)
    second = _seq("005930", start + timedelta(minutes=3), 3, base_close=80_000)

    store.write(first)
    store.write(second)

    out = list(store.read("005930", "1m"))
    assert len(out) == 6
    assert [b.close for b in out] == [70_000, 70_001, 70_002, 80_000, 80_001, 80_002]
    # Only one file (same year) — append merged into existing
    files = list((tmp_path / "bars" / "1m" / "005930").glob("*.parquet"))
    assert len(files) == 1


def test_read_streams_across_multiple_year_files(tmp_path):
    store = BarStore(tmp_path)
    bars = [
        _bar("005930", datetime(2024, 12, 31, tzinfo=UTC), 60_000),
        _bar("005930", datetime(2025, 6, 1, tzinfo=UTC), 70_000),
        _bar("005930", datetime(2026, 1, 2, tzinfo=UTC), 80_000),
    ]
    store.write(bars)
    out = list(store.read("005930", "1m"))
    assert [b.close for b in out] == [60_000, 70_000, 80_000]


def test_naive_input_treated_as_utc(tmp_path):
    """Bars constructed with a naive datetime should round-trip, with the
    read attaching UTC."""
    store = BarStore(tmp_path)
    naive_ts = datetime(2026, 5, 8, 9, 0)  # naive
    b = _bar("005930", naive_ts, 70_000)
    store.write([b])
    [back] = list(store.read("005930", "1m"))
    assert back.timestamp == naive_ts.replace(tzinfo=UTC)


def test_tz_aware_non_utc_input_normalized_to_utc(tmp_path):
    """A KST-aware timestamp should be converted to UTC at storage so the
    file is timezone-consistent."""
    from datetime import timezone

    store = BarStore(tmp_path)
    kst = timezone(timedelta(hours=9))
    kst_ts = datetime(2026, 5, 8, 18, 0, tzinfo=kst)  # 09:00 UTC
    b = _bar("005930", kst_ts, 70_000)
    store.write([b])

    [back] = list(store.read("005930", "1m"))
    assert back.timestamp == datetime(2026, 5, 8, 9, 0, tzinfo=UTC)


def test_year_boundary_split_into_two_files(tmp_path):
    store = BarStore(tmp_path)
    bars = [
        _bar("005930", datetime(2025, 12, 31, 23, 59, tzinfo=UTC), 70_000),
        _bar("005930", datetime(2026, 1, 1, 0, 0, tzinfo=UTC), 70_001),
    ]
    store.write(bars)
    assert (tmp_path / "bars" / "1m" / "005930" / "2025.parquet").exists()
    assert (tmp_path / "bars" / "1m" / "005930" / "2026.parquet").exists()


@pytest.mark.parametrize("count", [1, 100, 1000])
def test_volume_round_trip(tmp_path, count):
    store = BarStore(tmp_path)
    start = datetime(2026, 5, 8, 9, 0, tzinfo=UTC)
    bars = _seq("005930", start, count)
    assert store.write(bars) == count
    out = list(store.read("005930", "1m"))
    assert out == bars
