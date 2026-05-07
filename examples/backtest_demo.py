"""Demo: write synthetic daily bars to a BarStore, read them back, and
backtest a simple momentum strategy against them.

This is the integration smoke test for two modules together — `storage`
(Parquet round trip) and `backtest` (running the live Runtime / Allocator
code over historical bars). When the KIS REST poller is wired in, the
only line that changes is `generate_bars(...)` → real data.

Run:
    uv run examples/backtest_demo.py
"""

import random
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ks_ws.backtest.driver import BacktestDriver
from ks_ws.domain import Bar, Side, Signal
from ks_ws.storage.bars import BarStore
from ks_ws.strategies.allocator import Allocator
from ks_ws.strategies.base import Strategy

SYMBOL = "005930"  # 삼성전자
NUM_DAYS = 200
START_PRICE = 70_000
SEED = 42


def generate_bars(symbol: str, days: int, start_price: int, seed: int = 0) -> list[Bar]:
    """Random-walk daily OHLCV bars, deterministic by seed.

    Replace this function with real KIS REST output once an APP_KEY is
    available — the rest of the demo is unchanged.
    """
    rng = random.Random(seed)
    bars: list[Bar] = []
    base = datetime(2025, 5, 1, tzinfo=UTC)
    price = start_price
    for i in range(days):
        # Daily drift ±2%
        new_price = max(1, int(price + price * rng.uniform(-0.02, 0.02)))
        high = max(price, new_price) + rng.randint(0, 200)
        low = max(1, min(price, new_price) - rng.randint(0, 200))
        volume = rng.randint(500_000, 2_000_000)
        bars.append(
            Bar(
                symbol=symbol,
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
    return bars


class MomentumStrategy(Strategy):
    """Buy when today's close > yesterday's close; sell when lower.

    Trivial baseline. Lives in the demo (not promoted to public strategies/)
    until something else also uses it.
    """

    name = "momentum"

    def __init__(self) -> None:
        self._last_close: dict[str, int] = {}

    def on_bar(self, bar: Bar) -> list[Signal]:
        prev = self._last_close.get(bar.symbol)
        self._last_close[bar.symbol] = bar.close
        if prev is None:
            return []
        if bar.close > prev:
            side = Side.BUY
        elif bar.close < prev:
            side = Side.SELL
        else:
            return []
        return [
            Signal(
                symbol=bar.symbol,
                side=side,
                confidence=0.5,
                strategy=self.name,
                timestamp=bar.timestamp,
            )
        ]


def run_demo() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = BarStore(Path(tmp))
        bars = generate_bars(SYMBOL, NUM_DAYS, START_PRICE, seed=SEED)

        n_written = store.write(bars)
        print(f"Wrote {n_written} bars under {tmp}")

        loaded = list(store.read(SYMBOL, "1d"))
        print(f"Read back {len(loaded)} bars from store")
        first, last = loaded[0], loaded[-1]
        print(
            f"  range: {first.timestamp.date()} → {last.timestamp.date()} "
            f"(close {first.close:,} → {last.close:,})"
        )

        driver = BacktestDriver.from_strategies(
            loaded,
            [MomentumStrategy()],
            allocator=Allocator(max_position_per_symbol=100),
            starting_cash_krw=100_000_000,
        )
        result = driver.run()

        print(f"\n=== Backtest result ({SYMBOL}, {NUM_DAYS} 일) ===")
        print(f"  bars processed     {result.bars_processed:>14,}")
        print(f"  total trades       {result.total_trades:>14,}")
        print(f"  buys / sells       {result.total_buys:>6,} / {result.total_sells:<6,}")
        print(f"  winning sells      {result.winning_sells:>14,}")
        print(f"  win rate           {result.win_rate:>14.1%}")
        print(f"  realized PnL       {result.realized_pnl_krw:>14,} KRW")
        print(f"  unrealized PnL     {result.unrealized_pnl_krw:>14,} KRW")
        print(f"  total PnL          {result.total_pnl_krw:>14,} KRW")
        print(f"  cash               {result.cash_krw:>14,} KRW")
        print(f"  unfilled intents   {result.unfilled_intents:>14,}")


if __name__ == "__main__":
    run_demo()
