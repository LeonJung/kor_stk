"""foreign_trend — N일 누적 외인 trend score."""
from __future__ import annotations

import pytest

from ks_ws.sources.foreign_trend import (
    compute_trend_score,
    score_from_foreign_trend,
)

_TRILLION = 1_000_000_000_000


def test_empty_input_neutral_score() -> None:
    r = compute_trend_score([])
    assert r.score == 1.0
    assert r.direction == 0
    assert r.consistency == 0.0


def test_all_zero_input_neutral() -> None:
    r = compute_trend_score([0, 0, 0, 0, 0])
    assert r.score == 1.0
    assert r.direction == 0


def test_consistent_buy_trend_max_score() -> None:
    # 5 days x 0.5조 = 2.5조 total, all positive
    r = compute_trend_score([500_000_000_000] * 5, strong_n_threshold_krw=2 * _TRILLION)
    assert r.direction == 1
    assert r.consistency == 1.0
    assert r.sum_krw == 2_500_000_000_000
    assert r.score == pytest.approx(1.5)  # clamped from 1.0 + 0.5*1*1*1 + magnitude>1


def test_consistent_sell_trend_min_score() -> None:
    r = compute_trend_score([-500_000_000_000] * 5, strong_n_threshold_krw=2 * _TRILLION)
    assert r.direction == -1
    assert r.consistency == 1.0
    assert r.score == pytest.approx(0.5)  # 1.0 + 0.5*-1*1*1 = 0.5


def test_single_day_spike_vs_steady_diverge() -> None:
    # spike: 1 day +3조, 4 days 0 → consistency 0.2, magnitude 1.0
    spike = compute_trend_score(
        [0, 0, 0, 0, 3_000_000_000_000],
        strong_n_threshold_krw=2 * _TRILLION,
    )
    # steady: 5 days +0.4조 each = +2조 total
    steady = compute_trend_score(
        [400_000_000_000] * 5,
        strong_n_threshold_krw=2 * _TRILLION,
    )
    assert spike.score < steady.score  # steady should be stronger signal
    assert spike.consistency == pytest.approx(0.2)
    assert steady.consistency == pytest.approx(1.0)


def test_mixed_sign_low_consistency() -> None:
    # +1조, -1조, +1조, -1조, +0.1조 — net 약간 매수지만 4/5 가 sign 동의 안 함
    r = compute_trend_score(
        [_TRILLION, -_TRILLION, _TRILLION, -_TRILLION, 100_000_000_000],
        strong_n_threshold_krw=2 * _TRILLION,
    )
    # total > 0 → direction=+1, pos_days=3, n=5, consistency=0.6
    assert r.direction == 1
    assert r.consistency == pytest.approx(0.6)
    # score = 1 + 0.5 * 1 * min(0.1/2, 1) * 0.6 ≈ 1 + small → near 1.0
    assert 1.0 < r.score < 1.05


def test_score_helper_returns_score_only() -> None:
    score = score_from_foreign_trend([100_000_000_000] * 5)
    assert isinstance(score, float)
    assert 1.0 <= score <= 1.5


def test_invalid_threshold_raises() -> None:
    with pytest.raises(ValueError):
        compute_trend_score([100], strong_n_threshold_krw=0)


def test_score_clamped_to_zero() -> None:
    # extreme sell over multiple days
    r = compute_trend_score(
        [-_TRILLION * 10] * 5,  # -50조 total
        strong_n_threshold_krw=_TRILLION,
    )
    assert r.score == 0.5  # capped magnitude=1.0, direction=-1, consistency=1.0


def test_direction_zero_when_total_zero_mixed() -> None:
    # equal pos/neg, total = 0
    r = compute_trend_score([_TRILLION, -_TRILLION, 0])
    assert r.direction == 0
    assert r.score == 1.0
