"""Tests for stepped-anchor trailing exit (long-term strategies)."""

from __future__ import annotations

import pytest

from ks_ws.strategies._long_term_trailing import LongTermTrailingState


def test_below_100pct_holds_until_initial_sl():
    """100% 도달 전 entry 가까이 머무르면 hold. -20% 이탈 시 SL."""
    s = LongTermTrailingState(entry=100)
    s.update(120)
    assert s.should_exit(120) == ("hold", 80)
    s.update(85)
    assert s.should_exit(85) == ("hold", 80)
    s.update(80)
    assert s.should_exit(80) == ("sl", 80)


def test_below_100pct_sl_hit_below_floor():
    s = LongTermTrailingState(entry=100)
    s.update(70)
    assert s.should_exit(70) == ("sl", 80)


def test_at_200pct_anchor_activates():
    """entry × 2 도달 → anchor 200, trailing 160."""
    s = LongTermTrailingState(entry=100)
    s.update(220)
    assert s.max_anchor_n == 2
    # trailing = 200 × 0.8 = 160
    assert s.should_exit(220) == ("hold", 160)
    assert s.should_exit(165) == ("hold", 160)
    assert s.should_exit(160) == ("tp", 160)
    assert s.should_exit(155) == ("tp", 160)


def test_anchor_ratchets_up_only():
    """anchor 도달 후 더 높은 단계 도달 시 anchor 갱신."""
    s = LongTermTrailingState(entry=100)
    s.update(220)  # n=2 (220 // 100)
    assert s.max_anchor_n == 2
    s.update(180)  # 다시 떨어져도 anchor 유지
    assert s.max_anchor_n == 2
    s.update(330)  # n=3 → anchor 300
    assert s.max_anchor_n == 3
    # trailing = 300 × 0.8 = 240
    assert s.should_exit(245) == ("hold", 240)
    assert s.should_exit(240) == ("tp", 240)


def test_doc_example_e2e():
    """Doc 예시 그대로 reproduce."""
    s = LongTermTrailingState(entry=100)
    # 180 (80%) — anchor X
    s.update(180)
    assert s.should_exit(180) == ("hold", 80)
    # 220 (120%) → anchor n=2, trailing 160
    s.update(220)
    assert s.should_exit(220) == ("hold", 160)
    # 280 (180%) → still n=2
    s.update(280)
    assert s.should_exit(280) == ("hold", 160)
    # 320 (220%) → n=3, trailing 240
    s.update(320)
    assert s.should_exit(320) == ("hold", 240)
    # 245 → hold
    s.update(245)
    assert s.should_exit(245) == ("hold", 240)
    # 235 → tp
    assert s.should_exit(235) == ("tp", 240)


def test_custom_trailing_pct():
    """trailing_pct 10% 로 좁히면 anchor -10%."""
    s = LongTermTrailingState(entry=100, max_anchor_n=2)
    # anchor 200, trailing = 200 × 0.9 = 180
    assert s.should_exit(180, trailing_pct=10) == ("tp", 180)
    assert s.should_exit(190, trailing_pct=10) == ("hold", 180)


def test_custom_initial_sl_pct():
    """initial_sl_pct 30% → entry -30% 까지 hold."""
    s = LongTermTrailingState(entry=100)
    assert s.should_exit(75, initial_sl_pct=30) == ("hold", 70)
    assert s.should_exit(70, initial_sl_pct=30) == ("sl", 70)


def test_zero_entry_safe():
    """Edge: entry=0 이면 update no-op (divide-by-zero 안전)."""
    s = LongTermTrailingState(entry=0)
    s.update(100)  # 안 죽음
    assert s.max_anchor_n == 1
