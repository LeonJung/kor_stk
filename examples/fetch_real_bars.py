"""Fetch real KIS daily bars, persist them with BarStore, then backtest a
trivial momentum strategy on the round-tripped data.

Run (after .env is filled and verify_kis_token.py has worked):

    uv run examples/fetch_real_bars.py
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ks_ws.backtest.driver import BacktestDriver
from ks_ws.domain import Bar, Side, Signal
from ks_ws.market.kis_rest import fetch_daily_bars
from ks_ws.storage.bars import BarStore
from ks_ws.strategies.allocator import Allocator
from ks_ws.strategies.base import Strategy

SYMBOL = "005930"  # 삼성전자
DAYS_BACK = 100
DATA_DIR = Path(__file__).resolve().parents[1] / "data"


class _Momentum(Strategy):
    name = "momentum"

    def __init__(self) -> None:
        self._last_close: dict[str, int] = {}

    def on_bar(self, bar: Bar) -> list[Signal]:
        prev = self._last_close.get(bar.symbol)
        self._last_close[bar.symbol] = bar.close
        if prev is None:
            return []
        if bar.close == prev:
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


def main() -> None:
    today = datetime.now(UTC).date()
    start_date = today - timedelta(days=DAYS_BACK)

    print(f"Fetching {SYMBOL} daily bars from {start_date} to {today}...")
    fetched = fetch_daily_bars(SYMBOL, start=start_date, end=today)
    print(f"  got {len(fetched)} bars")
    if not fetched:
        print("  (no rows — check symbol or date range)")
        return

    print(f"\nWriting to {DATA_DIR}/bars/...")
    store = BarStore(DATA_DIR)
    written = store.write(fetched)
    print(f"  wrote {written} bars")

    loaded = list(store.read(SYMBOL, "1d"))
    print(f"  read back {len(loaded)} bars")
    print(
        f"  range: {loaded[0].timestamp.date()} → {loaded[-1].timestamp.date()} "
        f"(close {loaded[0].close:,} → {loaded[-1].close:,})"
    )

    print("\nRunning backtest with momentum strategy...")
    result = BacktestDriver.from_strategies(
        loaded,
        [_Momentum()],
        allocator=Allocator(max_position_per_symbol=100),
        starting_cash_krw=100_000_000,
    ).run()

    print(f"\n=== Backtest result ({SYMBOL}, {len(loaded)} 일) ===")
    print(f"  bars processed     {result.bars_processed:>14,}")
    print(f"  total trades       {result.total_trades:>14,}")
    print(f"  buys / sells       {result.total_buys:>6,} / {result.total_sells:<6,}")
    print(f"  win rate           {result.win_rate:>14.1%}")
    print(f"  realized PnL       {result.realized_pnl_krw:>14,} KRW")
    print(f"  unrealized PnL     {result.unrealized_pnl_krw:>14,} KRW")
    print(f"  total PnL          {result.total_pnl_krw:>14,} KRW")
    print(f"  cash               {result.cash_krw:>14,} KRW")
    print(f"  unfilled intents   {result.unfilled_intents:>14,}")


if __name__ == "__main__":
    main()
