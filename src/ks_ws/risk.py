"""Risk — gating layer between OrderIntent generation and broker submission.

The Risk layer is intentionally stateless (the ``Risk`` v1 class). It
inspects an intent against caller-supplied portfolio context (current
position for the symbol, realized PnL "today") and either approves it
(possibly with a reduced quantity) or rejects it (returns ``None``).

Two checks for v1:

- ``max_position_per_symbol``: gross-up cap on a single symbol. BUY
  intents that would push position past the cap are reduced. SELL is
  uncapped (you can always sell what you own).
- ``daily_loss_limit_krw``: circuit breaker. If the running realized
  PnL has dropped to ``-daily_loss_limit_krw`` or below, every new
  intent is rejected outright until the run resets the counter
  (backtest treats the entire run as one trading day; live mode resets
  daily). ``None`` disables the breaker.

For end-to-end live operation, ``EnhancedRisk`` chains the v1 ``Risk``
gate with ``LossResponseProtocol`` (Sec 20: 한 방 금지 / recovery_mode)
and ``PsychologyGuard`` (Sec 15, 19: 충동·복수 매매 차단). Each layer
returns either an adjusted intent or None; the chain composes left-to-
right so any rejection short-circuits.
"""

from ks_ws.domain import OrderIntent, Side
from ks_ws.loss_response import LossResponseProtocol
from ks_ws.psychology import PsychologyGuard


class Risk:
    def __init__(
        self,
        *,
        max_position_per_symbol: int = 100,
        daily_loss_limit_krw: int | None = 5_000_000,
    ) -> None:
        if max_position_per_symbol <= 0:
            raise ValueError("max_position_per_symbol must be positive")
        if daily_loss_limit_krw is not None and daily_loss_limit_krw <= 0:
            raise ValueError("daily_loss_limit_krw must be positive (or None to disable)")
        self.max_position_per_symbol = max_position_per_symbol
        self.daily_loss_limit_krw = daily_loss_limit_krw

    def check(
        self,
        intent: OrderIntent,
        *,
        current_position: int = 0,
        realized_pnl_today_krw: int = 0,
    ) -> OrderIntent | None:
        # Daily loss circuit breaker
        if (
            self.daily_loss_limit_krw is not None
            and realized_pnl_today_krw <= -self.daily_loss_limit_krw
        ):
            return None

        # Position cap (BUY only — sells reduce position, no need to gate)
        if intent.side == Side.BUY:
            allowed = max(0, self.max_position_per_symbol - current_position)
            if allowed == 0:
                return None
            if allowed < intent.quantity:
                return intent.model_copy(update={"quantity": allowed})

        return intent


class EnhancedRisk:
    """Composite risk gate: base Risk → LossResponseProtocol → PsychologyGuard.

    Chain semantics (left-to-right):
    1. ``Risk.check`` (position cap + daily loss circuit breaker)
    2. ``LossResponseProtocol.apply`` (cooldown / recovery scaling)
    3. ``PsychologyGuard.apply`` (revenge-trade / surge cooldown)

    Any layer returning None short-circuits and rejects the order.
    Quantity reductions compound naturally through the pipeline.

    Stateless across instances except the wrapped LossResponseProtocol /
    PsychologyGuard, which carry their own state. Caller is responsible
    for calling ``.record_trade(pnl_krw)`` on the loss protocol and
    ``.record_fill(symbol, side, pnl_krw)`` on the psychology guard
    after each fill is realized.
    """

    def __init__(
        self,
        *,
        risk: Risk,
        loss_protocol: LossResponseProtocol | None = None,
        psychology: PsychologyGuard | None = None,
    ) -> None:
        self.risk = risk
        self.loss_protocol = loss_protocol
        self.psychology = psychology

    def check(
        self,
        intent: OrderIntent,
        *,
        current_position: int = 0,
        realized_pnl_today_krw: int = 0,
    ) -> OrderIntent | None:
        out = self.risk.check(
            intent,
            current_position=current_position,
            realized_pnl_today_krw=realized_pnl_today_krw,
        )
        if out is None:
            return None
        if self.loss_protocol is not None:
            out = self.loss_protocol.apply(out, when=intent.timestamp)
            if out is None:
                return None
        if self.psychology is not None:
            out = self.psychology.apply(out, when=intent.timestamp)
            if out is None:
                return None
        return out
