"""LiveExecutor + EnhancedRisk end-to-end test (verifies type compatibility +
behavior with all guards stacked)."""

from datetime import UTC, datetime, timedelta

from ks_ws.bus import EventBus
from ks_ws.domain import OrderIntent, Side
from ks_ws.live import LiveExecutor
from ks_ws.loss_response import LossResponseProtocol
from ks_ws.orders import MockOrderRouter
from ks_ws.psychology import PsychologyGuard
from ks_ws.risk import EnhancedRisk, Risk


def _intent(side: Side = Side.BUY, qty: int = 10):
    return OrderIntent(
        symbol="X", side=side, quantity=qty, timestamp=datetime.now(UTC)
    )


def test_live_executor_accepts_enhanced_risk():
    bus = EventBus()
    enhanced = EnhancedRisk(
        risk=Risk(),
        loss_protocol=LossResponseProtocol(),
        psychology=PsychologyGuard(),
    )
    executor = LiveExecutor(bus, enhanced, MockOrderRouter())
    executor.setup()
    bus.publish(_intent())
    executor.step()
    assert len(executor.submitted) == 1


def test_enhanced_risk_blocks_during_cooldown():
    bus = EventBus()
    proto = LossResponseProtocol(max_single_loss_krw=500_000)
    enhanced = EnhancedRisk(risk=Risk(), loss_protocol=proto)
    executor = LiveExecutor(bus, enhanced, MockOrderRouter())
    executor.setup()
    proto.record_trade(pnl_krw=-1_000_000, when=datetime.now(UTC))
    bus.publish(_intent())
    executor.step()
    assert len(executor.submitted) == 0
    assert len(executor.rejected_by_risk) == 1


def test_enhanced_risk_psychology_revenge_block():
    bus = EventBus()
    psy = PsychologyGuard(revenge_block_duration=timedelta(hours=1))
    enhanced = EnhancedRisk(risk=Risk(), psychology=psy)
    executor = LiveExecutor(bus, enhanced, MockOrderRouter())
    executor.setup()
    psy.record_fill(symbol="X", side=Side.SELL, pnl_krw=-100_000)
    bus.publish(_intent(Side.BUY))
    executor.step()
    # SELL of X for closing position is allowed; new BUY is blocked
    assert len(executor.submitted) == 0
    # Now SELL should pass
    bus.publish(_intent(Side.SELL))
    executor.step()
    assert len(executor.submitted) == 1


def test_enhanced_risk_recovery_scales_quantity():
    bus = EventBus()
    proto = LossResponseProtocol(
        max_single_loss_krw=500_000,
        cooldown_duration=timedelta(seconds=30),
        recovery_pos_scale_initial=0.5,
    )
    enhanced = EnhancedRisk(risk=Risk(max_position_per_symbol=100), loss_protocol=proto)
    executor = LiveExecutor(bus, enhanced, MockOrderRouter())
    executor.setup()
    far_past = datetime.now(UTC) - timedelta(hours=2)
    proto.record_trade(pnl_krw=-1_000_000, when=far_past)
    # cooldown long over → recovery → 0.5 scale
    bus.publish(_intent(qty=20))
    executor.step()
    assert len(executor.submitted) == 1
    submitted_qty = executor.submitted[0].intent.quantity
    assert submitted_qty == 10  # 20 × 0.5
