"""Wilson score CI tests."""
from __future__ import annotations

import pytest

from ks_ws.stats.wilson_ci import is_better_than_random, wilson_ci


def test_small_n_wide_ci() -> None:
    # 4/5 = 80% point, but CI very wide
    ci = wilson_ci(4, 5)
    assert ci.point_estimate == 0.8
    assert ci.lower < 0.4  # 80% 같지만 lower 40% 이하 = 의미 X
    assert ci.upper > 0.95
    assert ci.width > 0.5


def test_large_n_narrow_ci() -> None:
    # 120/200 = 60% with narrow CI
    ci = wilson_ci(120, 200)
    assert abs(ci.point_estimate - 0.60) < 0.01
    assert 0.52 < ci.lower < 0.55
    assert 0.65 < ci.upper < 0.68
    assert ci.width < 0.15


def test_is_significantly_above_threshold() -> None:
    # 120/200 = 60% with CI ~ [53%, 67%]; lower > 50% → significant
    ci = wilson_ci(120, 200)
    assert ci.is_significantly_above(0.5)
    # 4/5 = 80% point but CI [38%, 96%] not above 50% with confidence
    ci_small = wilson_ci(4, 5)
    assert not ci_small.is_significantly_above(0.5)


def test_is_better_than_random_helper() -> None:
    assert is_better_than_random(120, 200) is True
    assert is_better_than_random(4, 5) is False
    # Edge: 100/200 = 50% point, lower < 50% → not significantly above
    assert is_better_than_random(100, 200) is False


def test_zero_wins() -> None:
    ci = wilson_ci(0, 100)
    assert ci.point_estimate == 0.0
    assert ci.lower == 0.0
    assert ci.upper < 0.05  # 적어도 5% 이하


def test_all_wins() -> None:
    ci = wilson_ci(100, 100)
    assert ci.point_estimate == 1.0
    assert ci.lower > 0.95
    assert ci.upper == pytest.approx(1.0)


def test_confidence_99_wider_than_95() -> None:
    ci_95 = wilson_ci(60, 100, 0.95)
    ci_99 = wilson_ci(60, 100, 0.99)
    assert ci_99.width > ci_95.width


def test_invalid_n() -> None:
    with pytest.raises(ValueError):
        wilson_ci(5, 0)


def test_invalid_wins() -> None:
    with pytest.raises(ValueError):
        wilson_ci(-1, 10)
    with pytest.raises(ValueError):
        wilson_ci(11, 10)


def test_invalid_confidence() -> None:
    with pytest.raises(ValueError):
        wilson_ci(50, 100, 0.80)


def test_walk_forward_strategy_validation_example() -> None:
    """실전 example: 삼각수렴 walk-forward 6 chunks 의 mean win=54%, n=180."""
    # 180 trades, 97 wins (54%)
    ci = wilson_ci(97, 180)
    # 95% CI 가 [47%, 61%] 정도 — 50% 와 거의 겹침
    # → 신뢰도 보통, but not significant alpha alone
    assert 0.45 < ci.lower < 0.50
    assert ci.upper > 0.60
    # Not significantly above 50%
    assert not ci.is_significantly_above(0.5)
    # But significantly above 45%
    assert ci.is_significantly_above(0.45)
