"""FundamentalAllocator — Pattern 1/2/4 결합 검증.

fundamental_strategy.md §3 의 universe filter + confidence boost + position sizing
조합이 동일 signal 셋에 macro_score 만 바꿔서 의도대로 동작하는지.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ks_ws.domain import Side, Signal
from ks_ws.strategies.fundamental_allocator import (
    FundamentalAllocator,
    score_from_foreign_flow_krw,
)


def _buy(symbol: str = "005930", *, confidence: float = 0.8, strategy: str = "breakout") -> Signal:
    return Signal(
        symbol=symbol,
        side=Side.BUY,
        confidence=confidence,
        strategy=strategy,
        timestamp=datetime.now(UTC),
    )


def _sell(symbol: str = "005930", *, confidence: float = 1.0, strategy: str = "breakout") -> Signal:
    return Signal(
        symbol=symbol,
        side=Side.SELL,
        confidence=confidence,
        strategy=strategy,
        timestamp=datetime.now(UTC),
    )


# --- score_from_foreign_flow_krw ---


def test_score_positive_strong() -> None:
    assert score_from_foreign_flow_krw(1_500_000_000) == 1.5
    assert score_from_foreign_flow_krw(1_000_000_000) == 1.5


def test_score_zero_is_neutral() -> None:
    assert score_from_foreign_flow_krw(0) == 1.0


def test_score_negative_strong_zero() -> None:
    assert score_from_foreign_flow_krw(-1_000_000_000) == 0.0
    assert score_from_foreign_flow_krw(-5_000_000_000) == 0.0


def test_score_interpolates() -> None:
    # +500M = halfway between 1.0 and 1.5 → 1.25
    assert score_from_foreign_flow_krw(500_000_000) == pytest.approx(1.25)
    # -500M = halfway between 1.0 and 0.0 → 0.5
    assert score_from_foreign_flow_krw(-500_000_000) == pytest.approx(0.5)


def test_score_invalid_threshold() -> None:
    with pytest.raises(ValueError):
        score_from_foreign_flow_krw(100, strong_threshold_krw=0)


# --- FundamentalAllocator.combine ---


def test_default_neutral_behaves_like_allocator() -> None:
    """macro_score=1.0 (default) → BUY 결과가 plain Allocator 와 동일 quantity."""
    alloc = FundamentalAllocator(max_position_per_symbol=100)
    intents = alloc.combine([_buy(confidence=0.8)])
    assert len(intents) == 1
    assert intents[0].side is Side.BUY
    # buy_score 0.8 * macro 1.0 = 0.8, mag 0.8 * min(1.0,1.0) = 0.8 → 80주
    assert intents[0].quantity == 80


def test_strong_macro_amplifies_size() -> None:
    """macro_score=1.5 → BUY score 1.2 cap 1.0, magnitude still scaled by min(1.5,1.0)=1.0."""
    alloc = FundamentalAllocator(max_position_per_symbol=100)
    alloc.set_macro_score("005930", 1.5)
    intents = alloc.combine([_buy(confidence=0.8)])
    assert len(intents) == 1
    # buy 0.8 * 1.5 = 1.2 → cap 1.0, magnitude 1.0 * min(1.5,1.0)=1.0 → 100주
    assert intents[0].quantity == 100


def test_weak_macro_attenuates_size() -> None:
    """macro_score=0.7 → BUY 통과하되 quantity 줄어듦."""
    alloc = FundamentalAllocator(max_position_per_symbol=100, min_score=0.5)
    alloc.set_macro_score("005930", 0.7)
    intents = alloc.combine([_buy(confidence=0.8)])
    assert len(intents) == 1
    # buy 0.8 * 0.7 = 0.56, mag 0.56 * min(0.7,1.0)=0.7 → 0.392 → 39주
    assert intents[0].quantity == 39


def test_below_min_score_blocks_buy() -> None:
    """macro_score < min_score → BUY signal 완전 무시."""
    alloc = FundamentalAllocator(max_position_per_symbol=100, min_score=0.5)
    alloc.set_macro_score("005930", 0.3)  # below 0.5
    intents = alloc.combine([_buy(confidence=0.9)])
    assert intents == []


def test_sell_always_passes_regardless_of_macro() -> None:
    """청산 (SELL) 은 macro_score 무관 — 이미 보유한 position 닫을 자유."""
    alloc = FundamentalAllocator(max_position_per_symbol=100, min_score=0.5)
    alloc.set_macro_score("005930", 0.1)  # very weak
    intents = alloc.combine([_sell(confidence=1.0)])
    assert len(intents) == 1
    assert intents[0].side is Side.SELL
    # sell magnitude 1.0, quantity 100 (no macro scaling on SELL)
    assert intents[0].quantity == 100


def test_buy_and_sell_same_symbol_net() -> None:
    """BUY 0.8 * macro 1.2 = 0.96 vs SELL 0.5. net = +0.46 → BUY."""
    alloc = FundamentalAllocator(max_position_per_symbol=100)
    alloc.set_macro_score("005930", 1.2)
    intents = alloc.combine([
        _buy(confidence=0.8),
        _sell(confidence=0.5),
    ])
    assert len(intents) == 1
    assert intents[0].side is Side.BUY
    # net = 0.96 - 0.5 = 0.46, mag 0.46 * min(1.2,1.0)=1.0 → 46주
    assert intents[0].quantity == 46


def test_blocked_buy_lets_sell_through() -> None:
    """BUY 가 macro veto 로 0 이 되어도 같은 종목의 SELL 은 통과."""
    alloc = FundamentalAllocator(max_position_per_symbol=100, min_score=0.5)
    alloc.set_macro_score("005930", 0.2)  # below threshold
    intents = alloc.combine([
        _buy(confidence=0.9),
        _sell(confidence=0.4),
    ])
    assert len(intents) == 1
    assert intents[0].side is Side.SELL
    # buy=0 (vetoed), sell=0.4, net=-0.4 → SELL 40주
    assert intents[0].quantity == 40


def test_invalid_min_score() -> None:
    with pytest.raises(ValueError):
        FundamentalAllocator(min_score=-0.1)


def test_invalid_macro_score() -> None:
    alloc = FundamentalAllocator()
    with pytest.raises(ValueError):
        alloc.set_macro_score("005930", -1.0)


def test_unknown_symbol_uses_default() -> None:
    """set_macro_score 안 한 종목 = default_score=1.0 적용 (plain Allocator 동일)."""
    alloc = FundamentalAllocator(max_position_per_symbol=100)
    intents = alloc.combine([_buy(symbol="000660", confidence=0.7)])
    assert len(intents) == 1
    assert intents[0].quantity == 70  # 0.7 * 1.0 * 1.0 = 0.7 → 70주


def test_weights_still_apply() -> None:
    """Allocator 의 strategy weight 가 macro 와 함께 동작."""
    alloc = FundamentalAllocator(max_position_per_symbol=100)
    alloc.set_weight("breakout", 2.0)
    alloc.set_macro_score("005930", 1.2)
    intents = alloc.combine([_buy(confidence=0.5, strategy="breakout")])
    assert len(intents) == 1
    # weight 2.0 * conf 0.5 = 1.0, * macro 1.2 = 1.2 → cap 1.0, mag 1.0 → 100주
    assert intents[0].quantity == 100
