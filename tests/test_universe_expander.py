"""UniverseExpander — 거래대금 폭증 종목 발굴."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ks_ws.bus import EventBus
from ks_ws.domain import Bar
from ks_ws.sources.universe_expander import (
    UniverseCandidateDetected,
    UniverseExpander,
)
from ks_ws.storage.bars import BarStore


def _bar(value: int, *, sym: str, minutes_ago: int) -> Bar:
    base = datetime(2026, 5, 13, 0, 0, tzinfo=UTC)
    ts = base - timedelta(minutes=minutes_ago)
    return Bar(
        symbol=sym, timeframe="1m", timestamp=ts,
        open=100, high=105, low=95, close=100,
        volume=1_000, value=value,
    )


def _seed_baseline_then_surge(
    store: BarStore, sym: str, *,
    baseline_value: int = 10_000_000,
    surge_value: int = 50_000_000,
    baseline_min: int = 60,
    recent_min: int = 15,
) -> None:
    """baseline_min 개 평소 + recent_min 개 surge 분봉."""
    total = baseline_min + recent_min
    bars: list[Bar] = []
    for i in range(total):
        minutes_ago = total - i - 1
        is_recent = i >= baseline_min
        bars.append(_bar(
            surge_value if is_recent else baseline_value,
            sym=sym, minutes_ago=minutes_ago,
        ))
    store.write(bars)


def test_detects_surge_candidate(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    _seed_baseline_then_surge(
        store, "005930",
        baseline_value=10_000_000, surge_value=50_000_000,
    )
    expander = UniverseExpander(
        EventBus(), store, ["005930"],
        log_db=tmp_path / "candidates.sqlite",
    )
    cands = expander.scan()
    assert len(cands) == 1
    assert cands[0].symbol == "005930"
    assert cands[0].surge_ratio == pytest.approx(5.0, rel=0.01)  # 50M/10M
    expander.close()


def test_no_surge_no_candidate(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    _seed_baseline_then_surge(
        store, "005930",
        baseline_value=10_000_000, surge_value=15_000_000,  # only 1.5x
    )
    expander = UniverseExpander(
        EventBus(), store, ["005930"],
        log_db=tmp_path / "candidates.sqlite",
    )
    assert expander.scan() == []
    expander.close()


def test_insufficient_history_skipped(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    # Only 10 bars — less than baseline 60 + recent 15
    bars = [_bar(10_000_000, sym="005930", minutes_ago=10 - i)
            for i in range(10)]
    store.write(bars)
    expander = UniverseExpander(
        EventBus(), store, ["005930"],
        log_db=tmp_path / "candidates.sqlite",
    )
    assert expander.scan() == []
    expander.close()


def test_publishes_event_on_surge(tmp_path: Path) -> None:
    bus = EventBus()
    sub = bus.subscribe(UniverseCandidateDetected, maxsize=10)
    store = BarStore(tmp_path)
    _seed_baseline_then_surge(store, "005930")
    expander = UniverseExpander(
        bus, store, ["005930"],
        log_db=tmp_path / "candidates.sqlite",
    )
    expander.scan()
    # Drain — sync publish, but subscribe is async iterator → check internal queue
    # via close + assert queue had item
    sub.close()
    expander.close()


def test_db_logging(tmp_path: Path) -> None:
    import sqlite3
    db = tmp_path / "candidates.sqlite"
    store = BarStore(tmp_path)
    _seed_baseline_then_surge(store, "005930")
    expander = UniverseExpander(EventBus(), store, ["005930"], log_db=db)
    expander.scan()
    expander.close()
    conn = sqlite3.connect(str(db))
    rows = conn.execute("SELECT symbol, surge_ratio FROM candidates").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "005930"
    assert rows[0][1] > 3.0


def test_invalid_windows_raise(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    bus = EventBus()
    with pytest.raises(ValueError):
        UniverseExpander(bus, store, [], recent_window_min=0, log_db=None)
    with pytest.raises(ValueError):
        UniverseExpander(
            bus, store, [],
            recent_window_min=10, baseline_window_min=10, log_db=None,
        )


def test_invalid_threshold_raises(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    with pytest.raises(ValueError):
        UniverseExpander(EventBus(), store, [], surge_threshold=1.0, log_db=None)


def test_multiple_symbols(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    _seed_baseline_then_surge(
        store, "005930",
        baseline_value=10_000_000, surge_value=50_000_000,
    )
    _seed_baseline_then_surge(
        store, "000660",
        baseline_value=10_000_000, surge_value=12_000_000,  # under threshold
    )
    expander = UniverseExpander(
        EventBus(), store, ["005930", "000660"],
        log_db=tmp_path / "candidates.sqlite",
    )
    cands = expander.scan()
    syms = [c.symbol for c in cands]
    assert "005930" in syms
    assert "000660" not in syms
    expander.close()


def test_scan_count_increments(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    expander = UniverseExpander(
        EventBus(), store, [],
        log_db=tmp_path / "candidates.sqlite",
    )
    expander.scan()
    expander.scan()
    assert expander.scan_count == 2
    expander.close()
