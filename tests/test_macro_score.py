"""macro_score blender — 다중 fundamental input 결합 검증."""
from __future__ import annotations

import pytest

from ks_ws.sources.macro_score import blend_macro_scores


def test_no_scores_neutral_fallback() -> None:
    assert blend_macro_scores() == 1.0


def test_single_score_returned_as_is() -> None:
    assert blend_macro_scores(1.3) == 1.3
    assert blend_macro_scores(0.5) == 0.5


def test_arithmetic_mean_two_inputs() -> None:
    """1.2 + 0.8 → mean 1.0."""
    assert blend_macro_scores(1.2, 0.8) == pytest.approx(1.0)


def test_arithmetic_mean_strong_combination() -> None:
    """외인 강(1.5) + RVOL 강(1.5) → 1.5 boost 유지."""
    assert blend_macro_scores(1.5, 1.5) == 1.5


def test_arithmetic_mean_one_strong_one_weak() -> None:
    """외인 강(1.5) + RVOL 약(0.3) → 0.9 (둘 다 신뢰 약화)."""
    assert blend_macro_scores(1.5, 0.3) == pytest.approx(0.9)


def test_weighted_mean() -> None:
    """외인 weight 2, RVOL weight 1 → 외인 영향 ↑."""
    # 1.5 * 2 + 0.6 * 1 = 3.6 / 3 = 1.2
    assert blend_macro_scores(1.5, 0.6, weights=[2, 1]) == pytest.approx(1.2)


def test_weighted_mean_zero_for_one_component() -> None:
    """한 component weight 0 → 다른 것만 반영."""
    assert blend_macro_scores(1.5, 0.0, weights=[1, 0]) == 1.5
    assert blend_macro_scores(1.5, 0.0, weights=[0, 1]) == 0.0


def test_cap_above_1_5() -> None:
    """입력이 1.5 초과해도 결과는 1.5 cap (방어적)."""
    assert blend_macro_scores(2.0, 2.5) == 1.5


def test_cap_below_zero() -> None:
    """음수 입력 들어와도 결과는 0.0 cap."""
    assert blend_macro_scores(-0.5, -0.3) == 0.0


def test_weights_length_mismatch() -> None:
    with pytest.raises(ValueError):
        blend_macro_scores(1.0, 1.0, weights=[1, 2, 3])


def test_weights_negative() -> None:
    with pytest.raises(ValueError):
        blend_macro_scores(1.0, 1.0, weights=[1, -1])


def test_weights_all_zero() -> None:
    with pytest.raises(ValueError):
        blend_macro_scores(1.0, 1.0, weights=[0, 0])


def test_three_components_typical() -> None:
    """외인 1.3 + RVOL 1.1 + regime 0.9 → mean 1.1."""
    assert blend_macro_scores(1.3, 1.1, 0.9) == pytest.approx(1.1)
