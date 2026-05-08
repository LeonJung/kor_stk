"""End-to-end live demo: KisMarketDataHub publishes Ticks to EventBus,
a subscriber prints them. Validates the full bus → hub wiring against
the real KIS WebSocket feed.

Run during KRX market hours (KST 09:00-15:30):

    uv run examples/live_realtime_demo.py [SYMBOL] [DURATION_SEC]
"""

import asyncio
import sys

from ks_ws.bus import EventBus
from ks_ws.domain import Tick
from ks_ws.market.hub import Tier
from ks_ws.market.kis_hub import KisMarketDataHub


async def run(symbol: str, duration_sec: int) -> None:
    bus = EventBus()
    sub = bus.subscribe(Tick)
    hub = KisMarketDataHub(bus)
    hub.assign(symbol, Tier.HOT)

    await hub.start()
    print(f"Hub started, subscribed to HOT: {symbol}\n")

    received = 0

    async def reader() -> None:
        nonlocal received
        async for tick in sub:
            received += 1
            ts = tick.timestamp.astimezone().strftime("%H:%M:%S")
            print(
                f"  Tick {received:>3}: {ts} {tick.symbol} "
                f"price={tick.price:,} vol={tick.volume:,}"
            )
            if received >= 30:
                break

    try:
        await asyncio.wait_for(reader(), timeout=duration_sec)
    except TimeoutError:
        print(f"\n(window {duration_sec}s elapsed)")

    print(f"\nTotal ticks received: {received}")
    await hub.stop()
    print("Hub stopped.")


def main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "005930"
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    asyncio.run(run(symbol, duration))


if __name__ == "__main__":
    main()
