"""review_log_analyze script — synthetic DB → 핵심 출력 sanity test."""
from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ks_ws.storage.trade_review import TradeReview, TradeReviewLog

_REPO = Path(__file__).parent.parent


def _seed_db(path: Path) -> None:
    log = TradeReviewLog(path)
    base = datetime(2026, 5, 13, 9, 0, tzinfo=UTC)
    # breakout: 2 wins, 1 loss → win_rate ~ 66%
    for i, (sym, entry, exit_p, reason, macro) in enumerate([
        ("005930", 100_000, 103_000, "TP", 1.2),
        ("000660", 50_000, 48_500, "SL", 0.7),
        ("373220", 200_000, 206_000, "TP", 1.1),
    ]):
        log.record(TradeReview(
            strategy="breakout", symbol=sym,
            entry_ts=base + timedelta(minutes=i * 5),
            entry_price=entry, qty=1,
            exit_ts=base + timedelta(minutes=i * 5 + 30),
            exit_price=exit_p, pnl_krw=exit_p - entry,
            exit_reason=reason, macro_score_at_entry=macro,
        ))
    # closing_bet: 1 win 1 loss
    log.record(TradeReview(
        strategy="closing_bet", symbol="005930",
        entry_ts=base, entry_price=100_000, qty=1,
        exit_ts=base + timedelta(hours=20),
        exit_price=102_000, pnl_krw=2_000,
        exit_reason="TP", macro_score_at_entry=1.0,
    ))
    log.record(TradeReview(
        strategy="closing_bet", symbol="000660",
        entry_ts=base, entry_price=50_000, qty=1,
        exit_ts=base + timedelta(hours=20),
        exit_price=48_500, pnl_krw=-1_500,
        exit_reason="SL", macro_score_at_entry=0.4,
    ))
    log.close()


def test_analyze_runs_on_synthetic_db(tmp_path: Path) -> None:
    db = tmp_path / "trade_review.sqlite"
    _seed_db(db)
    result = subprocess.run(
        [
            sys.executable, "-m", "scripts.review_log_analyze",
            "--db", str(db),
        ],
        cwd=_REPO,
        capture_output=True, text=True,
        env={
            "PYTHONPATH": str(_REPO / "src"),
            **{k: v for k, v in __import__("os").environ.items() if k != "PYTHONPATH"},
        },
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "신고가매매" in out
    assert "종가베팅" in out
    assert "macro_score_at_entry" in out
    assert "회고 후보" in out
    assert "보유 시간 분포" in out


def test_analyze_filter_by_strategy(tmp_path: Path) -> None:
    db = tmp_path / "trade_review.sqlite"
    _seed_db(db)
    result = subprocess.run(
        [
            sys.executable, "-m", "scripts.review_log_analyze",
            "--db", str(db), "--strategy", "breakout",
        ],
        cwd=_REPO,
        capture_output=True, text=True,
        env={
            "PYTHONPATH": str(_REPO / "src"),
            **{k: v for k, v in __import__("os").environ.items() if k != "PYTHONPATH"},
        },
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    out = result.stdout
    assert "신고가매매" in out
    assert "종가베팅" not in out  # filtered out


def test_analyze_empty_db_graceful(tmp_path: Path) -> None:
    db = tmp_path / "empty.sqlite"
    # Create empty schema
    log = TradeReviewLog(db)
    log.close()
    result = subprocess.run(
        [
            sys.executable, "-m", "scripts.review_log_analyze",
            "--db", str(db),
        ],
        cwd=_REPO,
        capture_output=True, text=True,
        env={
            "PYTHONPATH": str(_REPO / "src"),
            **{k: v for k, v in __import__("os").environ.items() if k != "PYTHONPATH"},
        },
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert "no rows" in result.stdout


def test_analyze_missing_db_graceful(tmp_path: Path) -> None:
    db = tmp_path / "absent.sqlite"
    result = subprocess.run(
        [
            sys.executable, "-m", "scripts.review_log_analyze",
            "--db", str(db),
        ],
        cwd=_REPO,
        capture_output=True, text=True,
        env={
            "PYTHONPATH": str(_REPO / "src"),
            **{k: v for k, v in __import__("os").environ.items() if k != "PYTHONPATH"},
        },
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert "no rows" in result.stdout
