"""Subscribe to KIS WebSocket realtime feed for a short window and print
parsed frames. Run during KRX market hours (KST 09:00-15:30) for actual
data.

Run:
    uv run examples/subscribe_realtime_demo.py [SYMBOL] [DURATION_SEC]
"""

import asyncio
import sys

from ks_ws.kis.realtime import KisRealtimeFeed


async def run(symbol: str, duration_sec: int) -> None:
    async with KisRealtimeFeed() as feed:
        print(f"Connected. approval_key={feed.approval_key[:12]}...")
        await feed.subscribe("H0STCNT0", symbol)  # 실시간 체결가
        print(f"Subscribed to H0STCNT0 (체결) on {symbol}, {duration_sec}s window\n")

        async def reader() -> None:
            count = 0
            async for raw in feed:
                tr_id, _enc, records = feed.parse_frame(raw)
                if tr_id == "":
                    # control / pingpong / subscription ack
                    print(f"  [ctrl] {raw[:100]}")
                    continue
                count += 1
                for rec in records:
                    if len(rec) < 3:
                        continue
                    # H0STCNT0 layout (subset): MKSC_SHRN_ISCD | STCK_CNTG_HOUR | STCK_PRPR | ...
                    sym, time_str, price = rec[0], rec[1], rec[2]
                    print(f"  [{tr_id}] {sym} {time_str} price={price}")
                if count >= 20:
                    break

        try:
            await asyncio.wait_for(reader(), timeout=duration_sec)
        except TimeoutError:
            print("\n(window elapsed)")
        await feed.unsubscribe("H0STCNT0", symbol)


def main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "005930"
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    asyncio.run(run(symbol, duration))


if __name__ == "__main__":
    main()
