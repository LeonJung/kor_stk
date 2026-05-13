"""GapPredictor — 다음날 갭 추정 + score 매핑."""
from __future__ import annotations

import pytest

from ks_ws.sources.gap_predictor import predict_gap_pct, score_from_predicted_gap

# --- predict_gap_pct ---


def test_no_inputs_returns_zero() -> None:
    assert predict_gap_pct() == 0.0


def test_single_us_market_only() -> None:
    """미장 +2% only → 2.0% (단일 component, weight irrelevant)."""
    assert predict_gap_pct(us_market_change_pct=2.0) == pytest.approx(2.0)


def test_us_market_negative() -> None:
    assert predict_gap_pct(us_market_change_pct=-3.0) == pytest.approx(-3.0)


def test_combined_us_and_cme() -> None:
    """미장 +2% + CME +1% with default weights us=0.35 cme=0.30.
    (2.0*0.35 + 1.0*0.30) / 0.65 = (0.7 + 0.3) / 0.65 = 1.538..
    """
    res = predict_gap_pct(us_market_change_pct=2.0, cme_overnight_change_pct=1.0)
    assert res == pytest.approx(1.0 / 0.65)


def test_all_four_components() -> None:
    """미장 +1, CME +0.5, after_hours +2, foreign +0.3 with default weights."""
    res = predict_gap_pct(
        us_market_change_pct=1.0,
        cme_overnight_change_pct=0.5,
        after_hours_change_pct=2.0,
        foreign_overnight_score_offset=0.3,
    )
    # Weighted sum: 1*0.35 + 0.5*0.30 + 2*0.20 + 0.3*0.15 = 0.35+0.15+0.4+0.045 = 0.945
    # Normalize: 0.945 / 1.0 = 0.945
    assert res == pytest.approx(0.945)


def test_custom_weights() -> None:
    """가중치 us=1.0 / cme=0.0 → 미장 만 반영."""
    res = predict_gap_pct(
        us_market_change_pct=3.0,
        cme_overnight_change_pct=-2.0,
        weights={"us": 1.0, "cme": 0.0, "after_hours": 0.0, "foreign": 0.0},
    )
    assert res == pytest.approx(3.0)


def test_invalid_negative_weights() -> None:
    with pytest.raises(ValueError):
        predict_gap_pct(
            us_market_change_pct=1.0,
            weights={"us": -0.5, "cme": 0.0, "after_hours": 0.0, "foreign": 0.0},
        )


def test_zero_weight_sum_returns_zero() -> None:
    """All weights 0 → 0.0 (graceful)."""
    assert predict_gap_pct(
        us_market_change_pct=2.0,
        weights={"us": 0.0, "cme": 0.0, "after_hours": 0.0, "foreign": 0.0},
    ) == 0.0


# --- score_from_predicted_gap ---


def test_score_strong_positive_gap() -> None:
    assert score_from_predicted_gap(2.0) == 1.3
    assert score_from_predicted_gap(5.0) == 1.3


def test_score_strong_negative_gap() -> None:
    assert score_from_predicted_gap(-2.0) == 0.7
    assert score_from_predicted_gap(-5.0) == 0.7


def test_score_neutral_zero() -> None:
    assert score_from_predicted_gap(0.0) == 1.0


def test_score_interpolation_positive() -> None:
    # +1% (50% of 2%) → 1.0 + 0.15 = 1.15
    assert score_from_predicted_gap(1.0) == pytest.approx(1.15)


def test_score_interpolation_negative() -> None:
    # -1% → 1.0 - 0.15 = 0.85
    assert score_from_predicted_gap(-1.0) == pytest.approx(0.85)


def test_score_custom_threshold() -> None:
    # strong_gap=5.0 → 5% gap = 1.3
    assert score_from_predicted_gap(5.0, strong_gap_pct=5.0) == 1.3
    # 2.5% with strong=5 → 1.15
    assert score_from_predicted_gap(2.5, strong_gap_pct=5.0) == pytest.approx(1.15)


def test_score_invalid_threshold() -> None:
    with pytest.raises(ValueError):
        score_from_predicted_gap(1.0, strong_gap_pct=0)
