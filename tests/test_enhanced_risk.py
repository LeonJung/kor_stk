"""Tests for EnhancedRisk composite gate."""

from datetime import UTC, datetime, timedelta

from ks_ws.domain import OrderIntent, Side
from ks_ws.loss_response import LossResponseProtocol
from ks_ws.psychology import PsychologyGuard
from ks_ws.risk import EnhancedRisk, Risk


def _ts(seconds: int = 0):
    return datetime(2026, 5, 11, 9, 0, tzinfo=UTC) + timedelta(seconds=seconds)


def _intent(qty: int = 10, side: Side = Side.BUY, ts=None) -> OrderIntent:
    return OrderIntent(symbol="X", side=side, quantity=qty, timestamp=ts or _ts(0))


def test_passes_through_when_all_clear():
    er = EnhancedRisk(
        risk=Risk(max_position_per_symbol=100),
        loss_protocol=LossResponseProtocol(),
        psychology=PsychologyGuard(),
    )
    out = er.check(_intent(10))
    assert out is not None
    assert out.quantity == 10


def test_base_risk_position_cap_reduces_qty():
    er = EnhancedRisk(
        risk=Risk(max_position_per_symbol=10),
        loss_protocol=LossResponseProtocol(),
        psychology=PsychologyGuard(),
    )
    out = er.check(_intent(20), current_position=5)
    assert out is not None
    assert out.quantity == 5  # 10 - 5


def test_base_risk_daily_loss_breaker_rejects():
    er = EnhancedRisk(
        risk=Risk(daily_loss_limit_krw=1_000_000),
        loss_protocol=LossResponseProtocol(),
        psychology=PsychologyGuard(),
    )
    out = er.check(_intent(10), realized_pnl_today_krw=-2_000_000)
    assert out is None


def test_loss_protocol_cooldown_blocks():
    proto = LossResponseProtocol(max_single_loss_krw=500_000)
    er = EnhancedRisk(risk=Risk(), loss_protocol=proto, psychology=PsychologyGuard())
    proto.record_trade(pnl_krw=-1_000_000, when=_ts(0))
    out = er.check(_intent(10, ts=_ts(60)))
    assert out is None  # cooldown blocks


def test_loss_protocol_recovery_scales_qty():
    proto = LossResponseProtocol(
        max_single_loss_krw=500_000,
        cooldown_duration=timedelta(seconds=30),
        recovery_pos_scale_initial=0.4,
    )
    er = EnhancedRisk(risk=Risk(), loss_protocol=proto, psychology=PsychologyGuard())
    proto.record_trade(pnl_krw=-1_000_000, when=_ts(0))
    out = er.check(_intent(10, ts=_ts(60)))  # past cooldown → recovery
    assert out is not None
    assert out.quantity == 4  # 10 × 0.4


def test_psychology_revenge_block():
    psy = PsychologyGuard(revenge_block_duration=timedelta(minutes=5))
    er = EnhancedRisk(risk=Risk(), loss_protocol=LossResponseProtocol(), psychology=psy)
    psy.record_fill(symbol="X", side=Side.SELL, pnl_krw=-100_000, when=_ts(0))
    out = er.check(_intent(10, ts=_ts(60)))
    assert out is None  # revenge-block


def test_chain_short_circuit_does_not_double_record():
    """If any layer returns None, downstream layers aren't asked → state not
    corrupted by partial failure."""
    psy = PsychologyGuard()
    er = EnhancedRisk(
        risk=Risk(max_position_per_symbol=1),
        loss_protocol=LossResponseProtocol(),
        psychology=psy,
    )
    # current_position=1 → allowed=0, intent rejected at base layer
    out = er.check(_intent(10), current_position=1)
    assert out is None
    # Psychology guard's history should NOT have been modified
    assert "X" not in psy._history


def test_optional_layers_are_skipped_when_none():
    er = EnhancedRisk(risk=Risk(), loss_protocol=None, psychology=None)
    out = er.check(_intent(10))
    assert out is not None
    assert out.quantity == 10


def test_sell_intents_pass_psychology_after_loss():
    psy = PsychologyGuard()
    er = EnhancedRisk(risk=Risk(), psychology=psy)
    psy.record_fill(symbol="X", side=Side.SELL, pnl_krw=-100_000, when=_ts(0))
    out = er.check(_intent(10, side=Side.SELL, ts=_ts(60)))
    assert out is not None  # SELL allowed even under revenge block
