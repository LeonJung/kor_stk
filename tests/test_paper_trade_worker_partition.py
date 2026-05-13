"""paper_trade_breakout._partition_universe — worker mode universe 분배."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from paper_trade_breakout import _partition_universe


def test_partition_two_workers_balanced() -> None:
    codes = ["A", "B", "C", "D", "E", "F"]
    w0 = _partition_universe(codes, 0, 2)
    w1 = _partition_universe(codes, 1, 2)
    assert w0 == ["A", "C", "E"]
    assert w1 == ["B", "D", "F"]
    assert set(w0) | set(w1) == set(codes)
    assert set(w0) & set(w1) == set()


def test_partition_single_worker_all() -> None:
    codes = ["A", "B", "C"]
    assert _partition_universe(codes, 0, 1) == codes


def test_partition_three_workers() -> None:
    codes = ["A", "B", "C", "D", "E", "F", "G"]
    w0 = _partition_universe(codes, 0, 3)
    w1 = _partition_universe(codes, 1, 3)
    w2 = _partition_universe(codes, 2, 3)
    assert w0 == ["A", "D", "G"]
    assert w1 == ["B", "E"]
    assert w2 == ["C", "F"]
    assert sorted(w0 + w1 + w2) == codes


def test_partition_empty_codes() -> None:
    assert _partition_universe([], 0, 1) == []
    assert _partition_universe([], 0, 2) == []


def test_partition_more_workers_than_codes() -> None:
    codes = ["A", "B"]
    w0 = _partition_universe(codes, 0, 5)
    w1 = _partition_universe(codes, 1, 5)
    w2 = _partition_universe(codes, 2, 5)
    assert w0 == ["A"]
    assert w1 == ["B"]
    assert w2 == []
