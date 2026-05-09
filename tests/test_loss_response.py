"""Tests for LossResponseProtocol."""

from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.domain import OrderIntent, Side
from ks_ws.loss_response import LossResponseProtocol


def _ts(seconds: float = 0):
    return datetime(2026, 5, 11, 9, 0, tzinfo=UTC) + timedelta(seconds=seconds)


def _intent(qty: int = 10) -> OrderIntent:
    return OrderIntent(
        symbol="X", side=Side.BUY, quantity=qty, timestamp=_ts(0), sources=("s",)
    )


def _proto(**overrides):
    defaults = dict(
        max_single_loss_krw=1_000_000,
        consecutive_loss_threshold=3,
        cooldown_duration=timedelta(hours=1),
        recovery_duration=timedelta(days=14),
        recovery_pos_scale_initial=0.3,
        recovery_pos_scale_step=0.2,
        recovery_step_period=timedelta(days=7),
        max_recovery_trades_per_day=3,
    )
    defaults.update(overrides)
    return LossResponseProtocol(**defaults)


# Validation ---------------------------------------------------------------


def test_validation():
    with pytest.raises(ValueError):
        LossResponseProtocol(max_single_loss_krw=0)
    with pytest.raises(ValueError):
        LossResponseProtocol(consecutive_loss_threshold=0)
    with pytest.raises(ValueError):
        LossResponseProtocol(recovery_pos_scale_initial=0)
    with pytest.raises(ValueError):
        LossResponseProtocol(recovery_pos_scale_step=2)
    with pytest.raises(ValueError):
        LossResponseProtocol(max_recovery_trades_per_day=0)


# Normal phase -------------------------------------------------------------


def test_normal_allows_full_quantity():
    p = _proto()
    intent = _intent(10)
    out = p.apply(intent, when=_ts(0))
    assert out is intent
    assert p.phase == "normal"


def test_small_loss_doesnt_trigger_cooldown():
    p = _proto(max_single_loss_krw=1_000_000)
    p.record_trade(pnl_krw=-500_000, when=_ts(0))
    assert p.phase == "normal"
    out = p.apply(_intent(10), when=_ts(60))
    assert out is not None and out.quantity == 10


# Cooldown trigger ---------------------------------------------------------


def test_single_big_loss_enters_cooldown():
    p = _proto(max_single_loss_krw=1_000_000)
    p.record_trade(pnl_krw=-1_500_000, when=_ts(0))
    assert p.phase == "cooldown"
    out = p.apply(_intent(10), when=_ts(60))
    assert out is None


def test_consecutive_losses_enter_cooldown():
    p = _proto(consecutive_loss_threshold=3)
    p.record_trade(pnl_krw=-100_000, when=_ts(0))
    p.record_trade(pnl_krw=-100_000, when=_ts(10))
    assert p.phase == "normal"
    p.record_trade(pnl_krw=-100_000, when=_ts(20))
    assert p.phase == "cooldown"


def test_winning_trade_resets_consecutive_streak():
    p = _proto(consecutive_loss_threshold=3)
    p.record_trade(pnl_krw=-100_000)
    p.record_trade(pnl_krw=-100_000)
    p.record_trade(pnl_krw=+50_000)  # reset
    p.record_trade(pnl_krw=-100_000)
    p.record_trade(pnl_krw=-100_000)
    assert p.phase == "normal"
    assert p.consecutive_losses == 2


# Recovery phase ----------------------------------------------------------


def test_recovery_starts_after_cooldown():
    p = _proto(
        cooldown_duration=timedelta(hours=1),
        recovery_duration=timedelta(days=14),
    )
    p.record_trade(pnl_krw=-1_500_000, when=_ts(0))
    # During cooldown
    assert p.advise(when=_ts(1800)).phase == "cooldown"
    # After cooldown → recovery
    advice = p.advise(when=_ts(3601))
    assert advice.phase == "recovery"
    assert 0 < advice.pos_scale < 1.0


def test_recovery_scales_up_weekly():
    p = _proto(
        cooldown_duration=timedelta(hours=1),
        recovery_duration=timedelta(days=30),
        recovery_pos_scale_initial=0.3,
        recovery_pos_scale_step=0.2,
        recovery_step_period=timedelta(days=7),
    )
    p.record_trade(pnl_krw=-1_500_000, when=_ts(0))
    after_cooldown = _ts(0) + timedelta(hours=1, seconds=60)
    a0 = p.advise(when=after_cooldown)
    assert a0.pos_scale == pytest.approx(0.3, abs=0.01)
    a1 = p.advise(when=after_cooldown + timedelta(days=7))
    assert a1.pos_scale == pytest.approx(0.5, abs=0.01)
    a2 = p.advise(when=after_cooldown + timedelta(days=14))
    assert a2.pos_scale == pytest.approx(0.7, abs=0.01)


def test_recovery_scales_quantity():
    p = _proto(
        cooldown_duration=timedelta(hours=1),
        recovery_pos_scale_initial=0.3,
    )
    p.record_trade(pnl_krw=-1_500_000, when=_ts(0))
    out = p.apply(_intent(10), when=_ts(0) + timedelta(hours=1, seconds=60))
    assert out is not None
    assert out.quantity == 3  # 10 × 0.3


def test_recovery_daily_trade_cap():
    p = _proto(
        cooldown_duration=timedelta(hours=1),
        max_recovery_trades_per_day=2,
    )
    p.record_trade(pnl_krw=-1_500_000, when=_ts(0))
    after = _ts(0) + timedelta(hours=1, seconds=60)
    # Use up the trade cap
    p.record_trade(pnl_krw=10_000, when=after)
    p.record_trade(pnl_krw=10_000, when=after + timedelta(seconds=10))
    advice = p.advise(when=after + timedelta(seconds=20))
    assert not advice.allow_trading
    assert "daily trade cap" in advice.reason


def test_returns_to_normal_after_recovery_completes():
    p = _proto(
        cooldown_duration=timedelta(hours=1),
        recovery_duration=timedelta(days=14),
    )
    p.record_trade(pnl_krw=-1_500_000, when=_ts(0))
    far_future = _ts(0) + timedelta(days=20)
    advice = p.advise(when=far_future)
    assert advice.phase == "normal"
    assert advice.pos_scale == 1.0


# apply integration --------------------------------------------------------


def test_apply_returns_none_during_cooldown():
    p = _proto()
    p.record_trade(pnl_krw=-2_000_000, when=_ts(0))
    assert p.apply(_intent(10), when=_ts(60)) is None
