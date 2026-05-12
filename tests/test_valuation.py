"""Valuation — PER/PBR score helpers."""
from __future__ import annotations

import pytest

from ks_ws.sources.valuation import (
    blend_per_pbr_score,
    score_from_pbr,
    score_from_per,
)

# --- score_from_per ---


def test_per_deep_value() -> None:
    assert score_from_per(5.0) == 1.3
    assert score_from_per(1.0) == 1.3
    assert score_from_per(0.1) == 1.3


def test_per_neutral_default() -> None:
    assert score_from_per(15.0) == 1.0


def test_per_high() -> None:
    assert score_from_per(50.0) == 0.7
    assert score_from_per(100.0) == 0.7


def test_per_30_anchor() -> None:
    assert score_from_per(30.0) == pytest.approx(0.85)


def test_per_interpolation_5_to_15() -> None:
    # halfway 10 → 1.15
    assert score_from_per(10.0) == pytest.approx(1.15)


def test_per_interpolation_15_to_30() -> None:
    # halfway 22.5 → 0.925
    assert score_from_per(22.5) == pytest.approx(0.925)


def test_per_negative() -> None:
    """적자기업 (PER<0) → 0.8 mild penalty."""
    assert score_from_per(-10.0) == 0.8


def test_per_none() -> None:
    """필드 없으면 neutral 1.0."""
    assert score_from_per(None) == 1.0


def test_per_custom_neutral() -> None:
    """neutral_per=20 이면 PER 20 = 1.0."""
    assert score_from_per(20.0, neutral_per=20.0) == 1.0


# --- score_from_pbr ---


def test_pbr_deep_value() -> None:
    assert score_from_pbr(0.5) == 1.3
    assert score_from_pbr(0.3) == 1.3


def test_pbr_book_value() -> None:
    assert score_from_pbr(1.0) == pytest.approx(1.15)


def test_pbr_2x() -> None:
    assert score_from_pbr(2.0) == pytest.approx(1.05)


def test_pbr_3x_neutral() -> None:
    assert score_from_pbr(3.0) == pytest.approx(1.0)


def test_pbr_high() -> None:
    assert score_from_pbr(5.0) == 0.9
    assert score_from_pbr(10.0) == 0.9


def test_pbr_none_or_negative() -> None:
    assert score_from_pbr(None) == 1.0
    assert score_from_pbr(0) == 1.0
    assert score_from_pbr(-1.0) == 1.0


# --- blend ---


def test_blend_typical() -> None:
    """삼성전자 5/13: PER 42.5 (~0.76) + PBR 4.36 (~0.93) → ~0.85."""
    # PER 42.5: 30→0.85, 50→0.7 / interpolate (42.5-30)/20 = 0.625 → 0.85 - 0.15*0.625 = 0.7563
    # PBR 4.36: 3→1.0, 5→0.9 / interp (4.36-3)/2 = 0.68 → 1.0 - 0.10*0.68 = 0.932
    expected = (0.7563 + 0.932) / 2
    assert blend_per_pbr_score(42.5, 4.36) == pytest.approx(expected, abs=0.005)


def test_blend_both_none() -> None:
    assert blend_per_pbr_score(None, None) == 1.0
