"""Live verification of KisFillFeed subscription + AES key capture.

Connects to KIS WS, subscribes to H0STCNI0 / H0STCNI9 keyed by
KIS_HTS_ID from .env, and listens for the configured window. Prints:
- whether the subscription ack arrived
- whether AES key + IV got captured for the tr_id
- any frames received (likely none unless an order fills during the
  window — this is normal)

A working setup yields the ack frame and "captured AES keys" log line
even with no orders happening. To exercise the decryption path
end-to-end, place a small mock order via place_test_order_demo
during the window.

Run:
    uv run examples/verify_fill_feed.py [DURATION_SEC]
"""

import asyncio
import contextlib
import logging
import sys

from ks_ws.config import get_settings
from ks_ws.kis.realtime import KisRealtimeFeed
from ks_ws.sources.fill_feed import FillEvent, KisFillFeed


async def run(duration_sec: int) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if not settings.hts_id:
        print("ERROR: KIS_HTS_ID is not set in .env — required for fill notifications.")
        return

    received: list[FillEvent] = []

    async with KisRealtimeFeed(settings) as feed:
        fill_feed = KisFillFeed(feed, received.append, settings)
        await fill_feed.subscribe()
        print(
            f"Subscribed to {fill_feed.tr_id} for hts_id={settings.hts_id}, "
            f"listening for {duration_sec}s..."
        )

        async def reader() -> None:
            async for raw in feed:
                fill_feed.handle_frame(raw)
                # Print first few frames for visibility
                if fill_feed.received <= 5:
                    print(f"  raw frame: {raw[:120]}")

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(reader(), timeout=duration_sec)

        print("\n=== Summary ===")
        print(f"  frames received:    {fill_feed.received}")
        print(f"  fill events parsed: {fill_feed.parsed}")
        print(f"  decrypt errors:     {fill_feed.errors}")
        print(f"  AES keys cached:    {sorted(feed._aes_keys.keys())}")


def main() -> None:
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    asyncio.run(run(duration))


if __name__ == "__main__":
    main()
