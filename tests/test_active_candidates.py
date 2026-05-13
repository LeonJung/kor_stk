"""ActiveCandidateRanker — universe_candidates.sqlite → top active."""
from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ks_ws.sources.active_candidates import top_active_candidates

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    surge_ratio REAL NOT NULL,
    recent_value_krw INTEGER NOT NULL,
    baseline_value_krw INTEGER NOT NULL,
    recent_minutes INTEGER NOT NULL
);
"""


def _seed(path: Path, rows: list[tuple[str, str, float]]) -> None:
    """rows = (detected_at_iso, symbol, surge_ratio)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO candidates (detected_at, symbol, surge_ratio, "
        "recent_value_krw, baseline_value_krw, recent_minutes) "
        "VALUES (?, ?, ?, 0, 0, 15)",
        rows,
    )
    conn.commit()
    conn.close()


def test_returns_empty_when_db_missing(tmp_path: Path) -> None:
    out = top_active_candidates(tmp_path / "absent.sqlite")
    assert out == []


def test_ranks_by_score(tmp_path: Path) -> None:
    db = tmp_path / "candidates.sqlite"
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    rows = [
        # A: 3 detections x 4.0 surge
        ((now - timedelta(hours=1)).isoformat(), "A", 4.0),
        ((now - timedelta(hours=2)).isoformat(), "A", 4.5),
        ((now - timedelta(hours=3)).isoformat(), "A", 3.8),
        # B: 1 detection x 10.0 surge (high but single)
        ((now - timedelta(hours=1)).isoformat(), "B", 10.0),
        # C: 5 detections x 3.5 surge (consistent)
        ((now - timedelta(hours=1)).isoformat(), "C", 3.5),
        ((now - timedelta(hours=2)).isoformat(), "C", 3.0),
        ((now - timedelta(hours=3)).isoformat(), "C", 3.2),
        ((now - timedelta(hours=4)).isoformat(), "C", 3.7),
        ((now - timedelta(hours=5)).isoformat(), "C", 3.4),
    ]
    _seed(db, rows)
    out = top_active_candidates(db, days=7, top_k=3, now_utc=now)
    # A: max=4.5, count=3, score=4.5*log(4)~6.24
    # B: max=10.0, count=1, score=10*log(2)~6.93
    # C: max=3.7, count=5, score=3.7*log(6)~6.63
    assert [c.symbol for c in out] == ["B", "C", "A"]
    assert out[0].max_surge_ratio == 10.0
    assert out[1].score == pytest.approx(3.7 * math.log(6), rel=0.01)


def test_filters_by_days_lookback(tmp_path: Path) -> None:
    db = tmp_path / "candidates.sqlite"
    now = datetime(2026, 5, 13, tzinfo=UTC)
    rows = [
        # 10 days ago — outside 7d window
        ((now - timedelta(days=10)).isoformat(), "OLD", 100.0),
        # 1 day ago — inside
        ((now - timedelta(days=1)).isoformat(), "FRESH", 4.0),
    ]
    _seed(db, rows)
    out = top_active_candidates(db, days=7, top_k=10, now_utc=now)
    syms = [c.symbol for c in out]
    assert "FRESH" in syms
    assert "OLD" not in syms


def test_excludes_provided_codes(tmp_path: Path) -> None:
    db = tmp_path / "candidates.sqlite"
    now = datetime(2026, 5, 13, tzinfo=UTC)
    rows = [
        ((now - timedelta(hours=1)).isoformat(), "005930", 5.0),
        ((now - timedelta(hours=1)).isoformat(), "NEW_A", 4.0),
    ]
    _seed(db, rows)
    out = top_active_candidates(
        db, days=7, top_k=10, exclude_codes={"005930"}, now_utc=now,
    )
    syms = [c.symbol for c in out]
    assert "005930" not in syms
    assert "NEW_A" in syms


def test_top_k_limits_results(tmp_path: Path) -> None:
    db = tmp_path / "candidates.sqlite"
    now = datetime(2026, 5, 13, tzinfo=UTC)
    rows = [
        ((now - timedelta(hours=i)).isoformat(), f"SYM{i:03d}",
         float(10 - i % 5))
        for i in range(20)
    ]
    _seed(db, rows)
    out = top_active_candidates(db, days=7, top_k=5, now_utc=now)
    assert len(out) == 5


def test_invalid_days_raises(tmp_path: Path) -> None:
    db = tmp_path / "candidates.sqlite"
    _seed(db, [])
    with pytest.raises(ValueError):
        top_active_candidates(db, days=0)


def test_invalid_top_k_raises(tmp_path: Path) -> None:
    db = tmp_path / "candidates.sqlite"
    _seed(db, [])
    with pytest.raises(ValueError):
        top_active_candidates(db, top_k=0)
