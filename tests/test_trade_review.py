"""TradeReviewLog tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ks_ws.storage.trade_review import TradeReview, TradeReviewLog


def _review(*, strategy: str = "breakout", symbol: str = "005930",
            pnl: int = 0, exit_reason: str = "TP",
            entry_price: int = 100_000, exit_price: int = 102_000,
            qty: int = 10) -> TradeReview:
    return TradeReview(
        strategy=strategy, symbol=symbol,
        entry_ts=datetime(2026, 5, 13, 9, 30, tzinfo=UTC),
        entry_price=entry_price, qty=qty,
        exit_ts=datetime(2026, 5, 13, 11, 15, tzinfo=UTC),
        exit_price=exit_price, pnl_krw=pnl, exit_reason=exit_reason,
        entry_note="breakout: 60d high cross",
        exit_note="TP @ +2%",
        macro_score_at_entry=0.85,
    )


def test_record_and_list(tmp_path: Path) -> None:
    log = TradeReviewLog(tmp_path / "review.sqlite")
    log.record(_review(pnl=20_000))
    reviews = log.list_reviews()
    assert len(reviews) == 1
    r = reviews[0]
    assert r["strategy"] == "breakout"
    assert r["symbol"] == "005930"
    assert r["pnl_krw"] == 20_000
    assert r["exit_reason"] == "TP"
    assert r["macro_score_at_entry"] == 0.85
    log.close()


def test_filter_by_strategy(tmp_path: Path) -> None:
    log = TradeReviewLog(tmp_path / "r.sqlite")
    log.record(_review(strategy="breakout", pnl=10_000))
    log.record(_review(strategy="closing_bet", pnl=-5_000))
    log.record(_review(strategy="double_bottom", pnl=8_000))
    breakout = log.list_reviews(strategy="breakout")
    assert len(breakout) == 1 and breakout[0]["strategy"] == "breakout"
    closing = log.list_reviews(strategy="closing_bet")
    assert len(closing) == 1 and closing[0]["pnl_krw"] == -5_000
    log.close()


def test_filter_by_symbol(tmp_path: Path) -> None:
    log = TradeReviewLog(tmp_path / "r.sqlite")
    log.record(_review(symbol="005930", pnl=10_000))
    log.record(_review(symbol="000660", pnl=-2_000))
    res = log.list_reviews(symbol="000660")
    assert len(res) == 1 and res[0]["symbol"] == "000660"
    log.close()


def test_per_strategy_summary(tmp_path: Path) -> None:
    log = TradeReviewLog(tmp_path / "r.sqlite")
    log.record(_review(strategy="breakout", pnl=20_000, exit_reason="TP"))
    log.record(_review(strategy="breakout", pnl=-5_000, exit_reason="SL"))
    log.record(_review(strategy="breakout", pnl=10_000, exit_reason="TP"))
    log.record(_review(strategy="closing_bet", pnl=-3_000, exit_reason="SL"))

    s = log.per_strategy_summary()
    assert s["breakout"]["count"] == 3
    assert s["breakout"]["total_pnl_krw"] == 25_000
    assert s["breakout"]["wins"] == 2
    assert s["breakout"]["losses"] == 1
    assert s["breakout"]["win_rate"] == pytest.approx(2 / 3)
    assert s["closing_bet"]["count"] == 1
    assert s["closing_bet"]["total_pnl_krw"] == -3_000
    log.close()


def test_invalid_qty(tmp_path: Path) -> None:
    log = TradeReviewLog(tmp_path / "r.sqlite")
    with pytest.raises(ValueError):
        log.record(_review(qty=0))
    with pytest.raises(ValueError):
        log.record(_review(qty=-1))
    log.close()


def test_persistence(tmp_path: Path) -> None:
    """Records persist across reopen."""
    path = tmp_path / "r.sqlite"
    log1 = TradeReviewLog(path)
    log1.record(_review(pnl=12_345))
    log1.close()
    log2 = TradeReviewLog(path)
    assert len(log2) == 1
    assert log2.list_reviews()[0]["pnl_krw"] == 12_345
    log2.close()


def test_order_by_exit_ts_desc(tmp_path: Path) -> None:
    log = TradeReviewLog(tmp_path / "r.sqlite")
    base = datetime(2026, 5, 13, 9, 0, tzinfo=UTC)
    for i, t_offset in enumerate([3, 1, 5, 2]):
        log.record(TradeReview(
            strategy="breakout", symbol=f"{i:06d}",
            entry_ts=base, entry_price=100, qty=1,
            exit_ts=base + timedelta(hours=t_offset),
            exit_price=110, pnl_krw=10, exit_reason="TP",
        ))
    reviews = log.list_reviews()
    # Most recent exit first
    exit_times = [r["exit_ts"] for r in reviews]
    assert exit_times == sorted(exit_times, reverse=True)
    log.close()


def test_limit(tmp_path: Path) -> None:
    log = TradeReviewLog(tmp_path / "r.sqlite")
    for i in range(5):
        log.record(_review(pnl=i * 1000))
    assert len(log.list_reviews(limit=3)) == 3
    with pytest.raises(ValueError):
        log.list_reviews(limit=0)
    log.close()
