"""Live verification of Hub COLD-tier batch fetch + BarStore write.

Spins up a KisMarketDataHub with one Tier.COLD symbol and a temp
BarStore, runs the batch task, then reads back to confirm the round
trip. Read-only against KIS (just inquire-daily-itemchartprice) — safe.

Run:
    uv run examples/verify_cold_batch.py [SYMBOL] [DAYS]
"""

import asyncio
import sys
import tempfile
from pathlib import Path

from ks_ws.bus import EventBus
from ks_ws.market.hub import Tier
from ks_ws.market.kis_hub import KisMarketDataHub
from ks_ws.storage.bars import BarStore


async def run(symbol: str, days: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = BarStore(Path(tmp))
        bus = EventBus()
        hub = KisMarketDataHub(bus, bar_store=store, cold_lookback_days=days)
        hub.assign(symbol, Tier.COLD)

        print(f"Running COLD batch: {symbol}, {days} days lookback...")
        # Run the cold batch task directly (no WS needed for COLD).
        await hub._cold_batch_load()

        bars = list(store.read(symbol, "1d"))
        print(f"\n  rows in store: {len(bars)}")
        if bars:
            first, last = bars[0], bars[-1]
            print(f"  range: {first.timestamp.date()} → {last.timestamp.date()}")
            print(f"  closes: {first.close:,} → {last.close:,}")
            print("\n  COLD batch + BarStore write verified.")
        else:
            print("\n  No rows returned — check tr_id / params.")


def main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "005930"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    asyncio.run(run(symbol, days))


if __name__ == "__main__":
    main()
