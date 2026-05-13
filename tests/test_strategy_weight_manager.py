"""StrategyWeightManager — review_log → allocator weight 자동 조정."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ks_ws.sources.strategy_weight_manager import (
    StrategyWeightManager,
    compute_strategy_weights,
)
from ks_ws.storage.trade_review import TradeReview, TradeReviewLog


class _StubAlloc:
    def __init__(self) -> None:
        self.weights: dict[str, float] = {}

    def set_weight(self, strategy_name: str, weight: float) -> None:
        self.weights[strategy_name] = weight


def _seed_trades(
    db: Path, *, strategy: str, wins: int, losses: int,
    days_ago: int = 1,
) -> None:
    log = TradeReviewLog(db)
    base = datetime(2026, 5, 13, tzinfo=UTC) - timedelta(days=days_ago)
    for i in range(wins):
        log.record(TradeReview(
            strategy=strategy, symbol="005930",
            entry_ts=base + timedelta(minutes=i * 5),
            entry_price=100, qty=1,
            exit_ts=base + timedelta(minutes=i * 5 + 30),
            exit_price=103, pnl_krw=3,
            exit_reason="TP",
        ))
    for i in range(losses):
        log.record(TradeReview(
            strategy=strategy, symbol="000660",
            entry_ts=base + timedelta(minutes=(wins + i) * 5),
            entry_price=100, qty=1,
            exit_ts=base + timedelta(minutes=(wins + i) * 5 + 30),
            exit_price=97, pnl_krw=-3,
            exit_reason="SL",
        ))
    log.close()


def test_high_winrate_gets_upper_weight(tmp_path: Path) -> None:
    db = tmp_path / "trade_review.sqlite"
    _seed_trades(db, strategy="breakout", wins=8, losses=2)  # 80% win rate
    out = compute_strategy_weights(db)
    assert len(out) == 1
    assert out[0].strategy == "breakout"
    assert out[0].weight == 1.2
    assert out[0].reason == "high_winrate"


def test_mid_winrate_gets_mid_weight(tmp_path: Path) -> None:
    db = tmp_path / "trade_review.sqlite"
    _seed_trades(db, strategy="breakout", wins=5, losses=5)  # 50%
    out = compute_strategy_weights(db)
    assert out[0].weight == 1.0
    assert out[0].reason == "ok"


def test_weak_winrate_gets_lower_weight(tmp_path: Path) -> None:
    db = tmp_path / "trade_review.sqlite"
    _seed_trades(db, strategy="breakout", wins=3, losses=7)  # 30%
    out = compute_strategy_weights(db)
    assert out[0].weight == 0.5
    assert out[0].reason == "weak"


def test_very_low_winrate_disabled(tmp_path: Path) -> None:
    db = tmp_path / "trade_review.sqlite"
    _seed_trades(db, strategy="breakout", wins=1, losses=9)  # 10%
    out = compute_strategy_weights(db)
    assert out[0].weight == 0.0
    assert out[0].reason == "disabled"


def test_insufficient_data_default_weight(tmp_path: Path) -> None:
    db = tmp_path / "trade_review.sqlite"
    _seed_trades(db, strategy="new_strategy", wins=1, losses=1)  # n=2 < n_min 5
    out = compute_strategy_weights(db, n_min=5)
    assert out[0].weight == 1.0
    assert out[0].reason == "insufficient"


def test_missing_strategies_filled_with_default(tmp_path: Path) -> None:
    db = tmp_path / "trade_review.sqlite"
    _seed_trades(db, strategy="breakout", wins=5, losses=5)
    out = compute_strategy_weights(
        db, strategies=["breakout", "closing_bet", "wedge"],
    )
    syms = {w.strategy: w for w in out}
    assert syms["closing_bet"].reason == "insufficient"
    assert syms["closing_bet"].weight == 1.0
    assert syms["wedge"].reason == "insufficient"


def test_lookback_days_filters_old_trades(tmp_path: Path) -> None:
    db = tmp_path / "trade_review.sqlite"
    _seed_trades(db, strategy="old", wins=10, losses=0, days_ago=30)  # > 14d
    out = compute_strategy_weights(db, days=14)
    assert out == []


def test_missing_db_returns_empty(tmp_path: Path) -> None:
    out = compute_strategy_weights(tmp_path / "absent.sqlite")
    assert out == []


def test_manager_applies_to_allocator(tmp_path: Path) -> None:
    db = tmp_path / "trade_review.sqlite"
    _seed_trades(db, strategy="breakout", wins=8, losses=2)
    _seed_trades(db, strategy="closing_bet", wins=1, losses=9)
    alloc = _StubAlloc()
    mgr = StrategyWeightManager(
        alloc, db, strategies=["breakout", "closing_bet", "wedge"],
    )
    weights = mgr.refresh()
    assert alloc.weights["breakout"] == 1.2  # high_winrate
    assert alloc.weights["closing_bet"] == 0.0  # disabled
    assert alloc.weights["wedge"] == 1.0  # insufficient default
    assert len(weights) == 3


def test_invalid_winrates_raise() -> None:
    with pytest.raises(ValueError):
        compute_strategy_weights(
            Path("/tmp/absent.sqlite"),
            lower_winrate=0.5, mid_winrate=0.4, upper_winrate=0.55,
        )


def test_invalid_days_raises() -> None:
    with pytest.raises(ValueError):
        compute_strategy_weights(Path("/tmp/absent.sqlite"), days=0)


def test_initial_weights_used_for_insufficient(tmp_path: Path) -> None:
    db = tmp_path / "trade_review.sqlite"
    # Seed n=2 trades for breakout (below n_min=5)
    _seed_trades(db, strategy="breakout", wins=1, losses=1)
    out = compute_strategy_weights(
        db, n_min=5,
        initial_weights={"breakout": 1.2, "wedge": 0.0},
        strategies=["breakout", "wedge", "closing_bet"],
    )
    syms = {w.strategy: w for w in out}
    assert syms["breakout"].weight == 1.2
    assert syms["breakout"].reason == "backtest_baseline"
    assert syms["wedge"].weight == 0.0
    assert syms["wedge"].reason == "backtest_baseline"
    assert syms["closing_bet"].weight == 1.0  # no initial → default
    assert syms["closing_bet"].reason == "insufficient"


def test_initial_weights_overridden_when_sufficient_live(tmp_path: Path) -> None:
    db = tmp_path / "trade_review.sqlite"
    # n >= 5 live data → live result overrides initial_weights
    _seed_trades(db, strategy="breakout", wins=8, losses=2)  # 80% win
    out = compute_strategy_weights(
        db, initial_weights={"breakout": 0.0},  # backtest said disable
    )
    assert out[0].weight == 1.2  # live overrides — high_winrate
    assert out[0].reason == "high_winrate"


def test_manager_initial_weights(tmp_path: Path) -> None:
    db = tmp_path / "trade_review.sqlite"
    alloc = _StubAlloc()
    mgr = StrategyWeightManager(
        alloc, db, strategies=["breakout", "wedge"],
        initial_weights={"breakout": 1.2, "wedge": 0.0},
    )
    mgr.refresh()
    assert alloc.weights["breakout"] == 1.2
    assert alloc.weights["wedge"] == 0.0
