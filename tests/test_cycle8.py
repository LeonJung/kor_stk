"""Tests for TrendShift / AttentionFlow / CrashRecovery / SeedRampUpGuard."""

from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.detectors.attention_flow import AttentionFlowDetector
from ks_ws.detectors.trend_shift import TrendShiftDetector
from ks_ws.domain import OrderIntent, Side, Tick
from ks_ws.events import ManiaSignal, TrendShift
from ks_ws.seed_ramp_guard import SeedRampUpGuard
from ks_ws.strategies.crash_recovery import CrashRecoveryStrategy


def _ts(seconds: int = 0):
    return datetime(2026, 5, 11, 9, 0, tzinfo=UTC) + timedelta(seconds=seconds)


# TrendShiftDetector ----------------------------------------------------


def test_trend_shift_validation():
    with pytest.raises(ValueError):
        TrendShiftDetector(emit=lambda e: None, expectancy_window=2)
    with pytest.raises(ValueError):
        TrendShiftDetector(emit=lambda e: None, drop_pct_threshold=0)


def test_trend_shift_emits_on_regime_change():
    events = []
    det = TrendShiftDetector(emit=events.append)
    det.update_regime("uptrend", when=_ts(0))
    assert events == []  # first update — no shift
    det.update_regime("sideways", when=_ts(60))
    assert len(events) == 1
    assert events[0].from_regime == "uptrend"
    assert events[0].to_regime == "sideways"


def test_trend_shift_no_emit_when_regime_unchanged():
    events = []
    det = TrendShiftDetector(emit=events.append)
    det.update_regime("uptrend", when=_ts(0))
    det.update_regime("uptrend", when=_ts(60))
    assert events == []


def test_trend_shift_emits_on_expectancy_drop():
    events = []
    det = TrendShiftDetector(emit=events.append, expectancy_window=10, drop_pct_threshold=30.0)
    det.update_regime("uptrend", when=_ts(0))
    # Prior half: avg 1000, recent half: avg 500 → drop 50%
    for i in range(5):
        det.update_expectancy(1000.0, when=_ts(i))
    for i in range(5):
        det.update_expectancy(500.0, when=_ts(i + 5))
    assert any(isinstance(e, TrendShift) and e.expectancy_drop_pct < 0 for e in events)


def test_trend_shift_no_emit_below_drop_threshold():
    events = []
    det = TrendShiftDetector(emit=events.append, expectancy_window=10, drop_pct_threshold=50.0)
    det.update_regime("uptrend", when=_ts(0))
    for i in range(5):
        det.update_expectancy(1000.0, when=_ts(i))
    for i in range(5):
        det.update_expectancy(800.0, when=_ts(i + 5))  # 20% drop, below 50
    assert events == []


# AttentionFlowDetector ------------------------------------------------


def test_attention_validation():
    with pytest.raises(ValueError):
        AttentionFlowDetector(emit=lambda e: None, score_threshold=2)


def test_attention_high_score_emits():
    events = []
    det = AttentionFlowDetector(emit=events.append, score_threshold=0.5)
    score = det.evaluate(
        symbol="X", turnover_krw=10**12, change_pct=25.0, news_count=30, when=_ts(0)
    )
    assert score >= 0.5
    assert len(events) == 1
    assert isinstance(events[0], ManiaSignal)


def test_attention_low_score_no_emit():
    events = []
    det = AttentionFlowDetector(emit=events.append, score_threshold=0.7)
    score = det.evaluate(
        symbol="X", turnover_krw=10**6, change_pct=1.0, news_count=0, when=_ts(0)
    )
    assert score < 0.7
    assert events == []


def test_attention_negative_change_counted_by_abs():
    events = []
    det = AttentionFlowDetector(emit=events.append, score_threshold=0.5)
    det.evaluate(
        symbol="X", turnover_krw=10**12, change_pct=-25.0, news_count=30, when=_ts(0)
    )
    assert len(events) == 1


# CrashRecoveryStrategy --------------------------------------------------


def test_crash_recovery_validation():
    with pytest.raises(ValueError):
        CrashRecoveryStrategy(large_cap_universe=set())
    with pytest.raises(ValueError):
        CrashRecoveryStrategy(large_cap_universe={"X"}, recovery_target_pct=0)


def test_crash_recovery_no_entry_when_no_panic():
    s = CrashRecoveryStrategy(large_cap_universe={"X"})
    sigs = s.on_tick(Tick(symbol="X", timestamp=_ts(0), price=10000, volume=10))
    assert sigs == []


def test_crash_recovery_entry_only_during_panic():
    s = CrashRecoveryStrategy(large_cap_universe={"X"})
    s.set_panic(True)
    sigs = s.on_tick(Tick(symbol="X", timestamp=_ts(0), price=9000, volume=10))
    assert len(sigs) == 1
    assert sigs[0].side == Side.BUY


def test_crash_recovery_skips_non_large_cap():
    s = CrashRecoveryStrategy(large_cap_universe={"BIG"})
    s.set_panic(True)
    sigs = s.on_tick(Tick(symbol="SMALL", timestamp=_ts(0), price=1000, volume=10))
    assert sigs == []


def test_crash_recovery_take_profit():
    s = CrashRecoveryStrategy(large_cap_universe={"X"}, recovery_target_pct=5.0)
    s.set_panic(True)
    s.on_tick(Tick(symbol="X", timestamp=_ts(0), price=9000, volume=10))
    sigs = s.on_tick(Tick(symbol="X", timestamp=_ts(60), price=9500, volume=10))  # +5.5%
    assert sigs and sigs[0].side == Side.SELL
    assert "TP" in sigs[0].note


def test_crash_recovery_stop_loss():
    s = CrashRecoveryStrategy(large_cap_universe={"X"}, stop_pct=3.0)
    s.set_panic(True)
    s.on_tick(Tick(symbol="X", timestamp=_ts(0), price=9000, volume=10))
    sigs = s.on_tick(Tick(symbol="X", timestamp=_ts(60), price=8700, volume=10))  # -3.3%
    assert sigs and sigs[0].side == Side.SELL
    assert sigs[0].urgency == "high"


# SeedRampUpGuard ------------------------------------------------------


def _intent(qty: int, side: Side = Side.BUY, ts=None):
    return OrderIntent(symbol="X", side=side, quantity=qty, timestamp=ts or _ts(0))


def test_ramp_validation():
    with pytest.raises(ValueError):
        SeedRampUpGuard(baseline_seed_krw=0, baseline_per_trade_krw=1)
    with pytest.raises(ValueError):
        SeedRampUpGuard(baseline_seed_krw=1, baseline_per_trade_krw=0)
    with pytest.raises(ValueError):
        SeedRampUpGuard(baseline_seed_krw=1, baseline_per_trade_krw=1, seed_jump_factor=0.5)


def test_ramp_starts_at_baseline():
    g = SeedRampUpGuard(
        baseline_seed_krw=20_000_000,
        baseline_per_trade_krw=1_000_000,
        per_trade_pct_of_seed=0.10,
    )
    cap = g.per_trade_cap_krw(when=_ts(0))
    # min(1M baseline, 0.10 × 20M = 2M) → 1M
    assert cap == 1_000_000


def test_ramp_grows_over_time():
    base = _ts(0)
    g = SeedRampUpGuard(
        baseline_seed_krw=20_000_000,
        baseline_per_trade_krw=1_000_000,
        ramp_step_krw=500_000,
        ramp_period=timedelta(days=7),
        per_trade_pct_of_seed=1.0,  # disable seed cap for this test
    )
    cap = g.per_trade_cap_krw(when=base + timedelta(days=14))
    # 2 ramp periods → +2 × 500_000 = +1M → 2M total
    assert cap == 2_000_000


def test_ramp_seed_cap_clamps():
    g = SeedRampUpGuard(
        baseline_seed_krw=10_000_000,
        baseline_per_trade_krw=5_000_000,  # already aggressive
        per_trade_pct_of_seed=0.10,
    )
    # 0.10 × 10M = 1M cap → less than 5M baseline
    assert g.per_trade_cap_krw(when=_ts(0)) == 1_000_000


def test_ramp_seed_jump_resets_baseline():
    g = SeedRampUpGuard(
        baseline_seed_krw=10_000_000,
        baseline_per_trade_krw=1_000_000,
        ramp_step_krw=500_000,
        ramp_period=timedelta(days=7),
        seed_jump_factor=2.0,
        per_trade_pct_of_seed=1.0,
    )
    # After 14 days, ramp would be +1M → cap = 2M
    base = _ts(0)
    g.update_seed(50_000_000, when=base + timedelta(days=14))  # 5x jump
    # Ramp should have reset → cap = 1M (baseline) at this moment
    cap = g.per_trade_cap_krw(when=base + timedelta(days=14, seconds=1))
    assert cap == 1_000_000


def test_ramp_caps_intent_quantity():
    g = SeedRampUpGuard(
        baseline_seed_krw=10_000_000,
        baseline_per_trade_krw=1_000_000,
        per_trade_pct_of_seed=1.0,
    )
    intent = _intent(qty=100)
    out = g.cap_intent(intent, price_per_share_krw=20_000)
    # 1M / 20k = 50 max qty
    assert out is not None
    assert out.quantity == 50


def test_ramp_sells_pass_through():
    g = SeedRampUpGuard(
        baseline_seed_krw=10_000_000, baseline_per_trade_krw=1_000_000,
    )
    intent = _intent(qty=100, side=Side.SELL)
    out = g.cap_intent(intent, price_per_share_krw=20_000)
    assert out is intent
