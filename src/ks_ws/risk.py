"""Risk — gating layer between OrderIntent generation and broker submission.

The Risk layer is intentionally stateless. It inspects an intent against
caller-supplied portfolio context (current position for the symbol,
realized PnL "today") and either approves it (possibly with a reduced
quantity) or rejects it (returns ``None``). State management belongs to
the caller — BacktestDriver tracks its own positions; a future Live
executor will reconcile with the broker.

Two checks for v1:

- ``max_position_per_symbol``: gross-up cap on a single symbol. BUY
  intents that would push position past the cap are reduced. SELL is
  uncapped (you can always sell what you own).
- ``daily_loss_limit_krw``: circuit breaker. If the running realized
  PnL has dropped to ``-daily_loss_limit_krw`` or below, every new
  intent is rejected outright until the run resets the counter
  (backtest treats the entire run as one trading day; live mode resets
  daily). ``None`` disables the breaker.
"""

from ks_ws.domain import OrderIntent, Side


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
