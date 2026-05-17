"""Tests for factor IC measurement."""

from __future__ import annotations

from datetime import date

import pytest

from ks_ws.research.factor_ic import (
    TEST_CUTOFF,
    compute_factor_ic,
    spearman_rank_correlation,
)


def test_spearman_perfect_correlation():
    assert spearman_rank_correlation([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]) == pytest.approx(1.0)


def test_spearman_perfect_anti():
    assert spearman_rank_correlation([1, 2, 3, 4, 5], [50, 40, 30, 20, 10]) == pytest.approx(-1.0)


def test_spearman_no_correlation():
    # truly orthogonal
    ic = spearman_rank_correlation([1, 2, 3, 4, 5], [3, 1, 5, 2, 4])
    assert -0.5 < ic < 0.5


def test_spearman_zero_variance():
    assert spearman_rank_correlation([1, 1, 1, 1, 1], [1, 2, 3, 4, 5]) == 0.0


def test_spearman_insufficient_data():
    assert spearman_rank_correlation([1, 2], [3, 4]) == 0.0


def test_spearman_with_ties():
    # ties → average rank
    ic = spearman_rank_correlation([1, 2, 2, 3], [1, 2, 2, 3])
    assert ic == pytest.approx(1.0)


def test_compute_factor_ic_predictive():
    """Strong positive correlation factor."""
    factor = {}
    ret = {}
    for i in range(30):  # 30 days
        d = date(2024, 1, 1).replace(day=(i % 28) + 1, month=(i // 28) + 1)
        factor[d] = {f"S{j}": j * 1.0 for j in range(50)}  # 0,1,...,49
        ret[d] = {f"S{j}": j * 0.5 + (i * 0.01) for j in range(50)}  # 같은 순위
    res = compute_factor_ic("strong", factor_by_day_sym=factor,
                            return_by_day_sym=ret, min_symbols_per_day=10)
    assert res.mean_ic > 0.9
    assert res.significant is True
    assert res.hit_rate == 1.0
    assert res.n_days == 30


def test_compute_factor_ic_no_predictive_power():
    """Random factor."""
    import random
    random.seed(42)
    factor = {}
    ret = {}
    for i in range(30):
        d = date(2024, 1, 1).replace(day=(i % 28) + 1, month=(i // 28) + 1)
        factor[d] = {f"S{j}": random.random() for j in range(50)}
        ret[d] = {f"S{j}": random.random() for j in range(50)}
    res = compute_factor_ic("random", factor_by_day_sym=factor,
                            return_by_day_sym=ret, min_symbols_per_day=10)
    assert abs(res.mean_ic) < 0.1
    assert res.significant is False


def test_train_only_filters_test_period():
    """test_cutoff 이후 데이터 사용 X."""
    factor = {
        date(2025, 1, 1): {"A": 1.0, "B": 2.0, "C": 3.0},
        date(2025, 6, 1): {"A": 1.0, "B": 2.0, "C": 3.0},
        date(2026, 1, 1): {"A": 1.0, "B": 2.0, "C": 3.0},  # after cutoff
    }
    ret = {
        date(2025, 1, 1): {"A": 1.0, "B": 2.0, "C": 3.0},
        date(2025, 6, 1): {"A": 1.0, "B": 2.0, "C": 3.0},
        date(2026, 1, 1): {"A": 1.0, "B": 2.0, "C": 3.0},
    }
    res = compute_factor_ic("test", factor_by_day_sym=factor,
                            return_by_day_sym=ret, min_symbols_per_day=3,
                            train_only=True)
    assert res.n_days == 2  # cutoff 이후 1일 제외


def test_min_symbols_threshold():
    factor = {date(2025, 1, 1): {"A": 1.0, "B": 2.0}}  # only 2 symbols
    ret = {date(2025, 1, 1): {"A": 1.0, "B": 2.0}}
    res = compute_factor_ic("few", factor_by_day_sym=factor,
                            return_by_day_sym=ret, min_symbols_per_day=10)
    assert res.n_days == 0  # below threshold → skip


def test_test_cutoff_constant():
    assert TEST_CUTOFF == date(2025, 8, 1)
