"""Side-by-side backtest: same strategy, with and without KRX retail
costs (commission + sell tax + slippage).

Demonstrates how default-cost backtesting reduces apparent PnL versus
the cost-free idealized result. Useful for calibrating expectations
before going live.

Run:
    uv run examples/fees_comparison_demo.py
"""

import random
from datetime import UTC, datetime, timedelta

from ks_ws.backtest.driver import BacktestDriver
from ks_ws.domain import Bar, Side, Signal
from ks_ws.strategies.allocator import Allocator
from ks_ws.strategies.base import Strategy

SEED = 7
DAYS = 250
START_PRICE = 70_000


def _bars(seed: int) -> list[Bar]:
    rng = random.Random(seed)
    base = datetime(2025, 1, 2, tzinfo=UTC)
    price = START_PRICE
    out: list[Bar] = []
    for i in range(DAYS):
        new_price = max(1, int(price + price * rng.uniform(-0.02, 0.02)))
        high = max(price, new_price) + rng.randint(0, 200)
        low = max(1, min(price, new_price) - rng.randint(0, 200))
        volume = rng.randint(500_000, 2_000_000)
        out.append(
            Bar(
                symbol="005930",
                timestamp=base + timedelta(days=i),
                timeframe="1d",
                open=price,
                high=high,
                low=low,
                close=new_price,
                volume=volume,
                value=new_price * volume,
            )
        )
        price = new_price
    return out


class _Momentum(Strategy):
    """Same trivial momentum strategy used in backtest_demo.py."""

    name = "momentum"

    def __init__(self) -> None:
        self._last_close: dict[str, int] = {}

    def on_bar(self, bar: Bar) -> list[Signal]:
        prev = self._last_close.get(bar.symbol)
        self._last_close[bar.symbol] = bar.close
        if prev is None or bar.close == prev:
            return []
        return [
            Signal(
                symbol=bar.symbol,
                side=Side.BUY if bar.close > prev else Side.SELL,
                confidence=0.5,
                strategy=self.name,
                timestamp=bar.timestamp,
            )
        ]


def _run(name: str, **driver_kwargs) -> None:
    bars = _bars(SEED)
    driver = BacktestDriver.from_strategies(
        bars,
        [_Momentum()],
        allocator=Allocator(max_position_per_symbol=100),
        starting_cash_krw=100_000_000,
        **driver_kwargs,
    )
    r = driver.run()
    total = r.realized_pnl_krw + r.unrealized_pnl_krw
    print(
        f"  {name:<28} realized {r.realized_pnl_krw:>+12,}  "
        f"unrealized {r.unrealized_pnl_krw:>+12,}  "
        f"total {total:>+12,}  "
        f"costs {r.total_costs_krw:>9,}  "
        f"win_rate {r.win_rate:>5.1%}"
    )


def main() -> None:
    print("=== KRX retail cost impact (momentum strategy, 250 days) ===\n")
    _run("no costs", commission_bps=0, sell_tax_bps=0)
    _run("commission only (1.5 bps)", commission_bps=1.5, sell_tax_bps=0)
    _run("commission + sell tax", commission_bps=1.5, sell_tax_bps=18)
    _run("+ 10 bps slippage", commission_bps=1.5, sell_tax_bps=18, slippage_bps=10)
    _run("+ 25 bps slippage", commission_bps=1.5, sell_tax_bps=18, slippage_bps=25)


if __name__ == "__main__":
    main()
