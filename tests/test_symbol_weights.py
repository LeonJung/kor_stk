"""Tests for SymbolWeightMatrix + Allocator 통합."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ks_ws.domain import Side, Signal
from ks_ws.strategies.allocator import Allocator
from ks_ws.strategies.symbol_weights import (
    SymbolWeightMatrix,
    WeightRule,
    compute_weight,
)


def test_compute_weight_rules():
    # min_n=3 미달 → 0
    assert compute_weight(n=2, wins=2, pnl_pct_sum=2.0) == 0.0
    # avg ≤ 0 → 0
    assert compute_weight(n=5, wins=3, pnl_pct_sum=-1.0) == 0.0
    # 60%+ → 3.0
    assert compute_weight(n=5, wins=4, pnl_pct_sum=5.0) == 3.0
    # 50% → 2.0
    assert compute_weight(n=10, wins=5, pnl_pct_sum=5.0) == 2.0
    # 40% → 1.0
    assert compute_weight(n=10, wins=4, pnl_pct_sum=5.0) == 1.0
    # < 40% → 0
    assert compute_weight(n=10, wins=3, pnl_pct_sum=5.0) == 0.0


def test_custom_weight_rule():
    r = WeightRule(min_n=5, w_60=5.0)
    assert compute_weight(n=4, wins=4, pnl_pct_sum=4.0, rule=r) == 0.0  # n < 5
    assert compute_weight(n=5, wins=3, pnl_pct_sum=5.0, rule=r) == 5.0  # 60%


def test_matrix_load_empty(tmp_path):
    m = SymbolWeightMatrix(db_path=str(tmp_path / "w.sqlite"))
    assert m.load() == 0
    assert m.weight_for("strat", "005930") == 1.0  # default


def test_matrix_upsert_and_query(tmp_path):
    m = SymbolWeightMatrix(db_path=str(tmp_path / "w.sqlite"))
    m.upsert("breakout", "005930", 3.0, train_n=10, train_wins=8,
             train_pnl_pct_sum=15.0)
    assert m.weight_for("breakout", "005930") == 3.0
    # 다른 strategy / 종목 = default
    assert m.weight_for("breakout", "000660") == 1.0
    assert m.weight_for("other", "005930") == 1.0

    # Reload from disk
    m2 = SymbolWeightMatrix(db_path=str(tmp_path / "w.sqlite"))
    m2.load()
    assert m2.weight_for("breakout", "005930") == 3.0


def test_matrix_bulk_upsert(tmp_path):
    m = SymbolWeightMatrix(db_path=str(tmp_path / "w.sqlite"))
    m.bulk_upsert([
        ("strat", "A", 3.0, 5, 4, 5.0),
        ("strat", "B", 0.0, 5, 1, 1.0),
        ("strat", "C", 2.0, 5, 3, 5.0),
    ])
    assert m.weight_for("strat", "A") == 3.0
    assert m.weight_for("strat", "B") == 0.0
    assert m.weight_for("strat", "C") == 2.0


def test_allocator_with_symbol_weights(tmp_path):
    m = SymbolWeightMatrix(db_path=str(tmp_path / "w.sqlite"))
    m.upsert("breakout", "GOOD", 3.0)
    m.upsert("breakout", "BLOCK", 0.0)

    alloc = Allocator(max_position_per_symbol=100, symbol_weights=m)
    ts = datetime.now(UTC)
    signals = [
        Signal(symbol="GOOD", side=Side.BUY, confidence=0.5,
               strategy="breakout", timestamp=ts),
        Signal(symbol="BLOCK", side=Side.BUY, confidence=0.5,
               strategy="breakout", timestamp=ts),
        Signal(symbol="NEW", side=Side.BUY, confidence=0.5,
               strategy="breakout", timestamp=ts),
    ]
    intents = alloc.combine(signals)
    by_sym = {i.symbol: i for i in intents}

    # GOOD: weight 3 → quantity ≈ 0.5 × 3 × 100 = 150
    assert "GOOD" in by_sym
    assert by_sym["GOOD"].quantity == 150
    # BLOCK: weight 0 → 차단
    assert "BLOCK" not in by_sym
    # NEW (not in matrix): default 1.0 → 50
    assert "NEW" in by_sym
    assert by_sym["NEW"].quantity == 50


def test_allocator_sell_ignores_symbol_weight(tmp_path):
    """SELL 은 청산이라 weight 0 이라도 emit 되어야."""
    m = SymbolWeightMatrix(db_path=str(tmp_path / "w.sqlite"))
    m.upsert("breakout", "X", 0.0)
    alloc = Allocator(max_position_per_symbol=100, symbol_weights=m)
    ts = datetime.now(UTC)
    intents = alloc.combine([
        Signal(symbol="X", side=Side.SELL, confidence=1.0,
               strategy="breakout", timestamp=ts),
    ])
    assert len(intents) == 1
    assert intents[0].side == Side.SELL


def test_allocator_no_weights_default_behavior(tmp_path):
    """symbol_weights=None 시 기존 동작 유지."""
    alloc = Allocator(max_position_per_symbol=100)
    ts = datetime.now(UTC)
    intents = alloc.combine([
        Signal(symbol="A", side=Side.BUY, confidence=0.5,
               strategy="s", timestamp=ts),
    ])
    assert len(intents) == 1
    assert intents[0].quantity == 50


def test_matrix_stats(tmp_path):
    m = SymbolWeightMatrix(db_path=str(tmp_path / "w.sqlite"))
    m.bulk_upsert([
        ("s", "A", 3.0, 5, 4, 5.0),
        ("s", "B", 2.0, 5, 3, 3.0),
        ("s", "C", 0.0, 5, 1, 0.5),
        ("s", "D", 1.0, 5, 2, 2.0),
    ])
    stats = m.stats()
    assert stats["s"]["total"] == 4
    assert stats["s"]["blocked"] == 1
    assert stats["s"]["x3+"] == 1
    assert stats["s"]["x2"] == 1
    assert stats["s"]["x1"] == 1
