"""Verify KIS H0STASP0 (호가) realtime frame 수신 — paper_trade 와
별도 process 로 30초 동안 frame stat 출력.

paper_trade_breakout 가 orderbook=0 으로 누적되는 원인 진단용:
- ack JSON (subscription 응답) 출력 → KIS 가 H0STASP0 구독 수락했나 확인
- 30초 동안 tr_id 별 frame count → 실제 호가 frame 오나 확인
- 만일 0 이면 KIS mock 서버가 H0STASP0 미지원

Usage:
    PYTHONPATH=src .venv/bin/python -m examples.verify_orderbook [SYMBOL] [DURATION]
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def main() -> int:
    from ks_ws.config import get_settings
    from ks_ws.kis.realtime import KisRealtimeFeed

    symbol = sys.argv[1] if len(sys.argv) > 1 else "005930"
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 30

    settings = get_settings()
    feed = KisRealtimeFeed(settings)

    counts: dict[str, int] = {"H0STCNT0": 0, "H0STASP0": 0, "ack": 0, "pingpong": 0, "other": 0}
    acks: list[str] = []

    async with feed:
        await feed.subscribe("H0STCNT0", symbol)
        await feed.subscribe("H0STASP0", symbol)
        print(f"\nSubscribed H0STCNT0 + H0STASP0 for {symbol}, window={duration}s\n")

        async def reader() -> None:
            async for raw in feed:
                if raw.startswith("{"):
                    if "PINGPONG" in raw:
                        counts["pingpong"] += 1
                    else:
                        counts["ack"] += 1
                        acks.append(raw[:300])
                    continue
                try:
                    tr_id, _enc, _records = feed.parse_frame(raw)
                except Exception:
                    counts["other"] += 1
                    continue
                if tr_id in ("H0STCNT0", "H0STASP0"):
                    counts[tr_id] += 1
                else:
                    counts["other"] += 1

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(reader(), timeout=duration)

    print(f"\n=== {duration}s frame stat ===")
    for k, v in counts.items():
        print(f"  {k:12s}: {v}")
    print("\n=== subscription acks (first 3) ===")
    for a in acks[:3]:
        print(a)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
