"""Fetch the current price and 10-deep orderbook for a symbol.

Run:
    uv run examples/fetch_quote_demo.py [SYMBOL]
"""

import sys
import time

from ks_ws.market.kis_rest import fetch_current_price, fetch_orderbook


def main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "005930"

    p = fetch_current_price(symbol)
    print(f"=== Current price ({symbol}) ===")
    print(f"  price:        {p.price:>12,} KRW")
    print(f"  open / high / low: {p.open:,} / {p.high:,} / {p.low:,}")
    print(f"  prev close:   {p.prev_close:>12,} KRW")
    print(f"  change:       {p.change:>+12,} KRW ({p.change_pct:+.2f}%)")
    print(f"  volume:       {p.volume:>12,} 주")
    print(f"  value:        {p.value:>12,} KRW")
    print(f"  timestamp:    {p.timestamp.isoformat()}")

    # KIS 모의투자 rate limit (~2 req/sec) — 짧은 sleep 으로 회피.
    # 프로덕션에선 client 차원의 rate limiter 필요.
    time.sleep(0.5)

    ob = fetch_orderbook(symbol)
    print(f"\n=== Orderbook ({symbol}) ===")
    print(f"  {'ASK':>14} {'BID':>14}")
    # Show ask in reverse (best ask at the bottom, near the spread) for clarity
    levels = max(len(ob.bids), len(ob.asks))
    for i in reversed(range(levels)):
        ap = ob.asks[i].price if i < len(ob.asks) else None
        av = ob.asks[i].volume if i < len(ob.asks) else None
        right = f"{ap:>8,} ({av:>5,})" if ap else " " * 16
        print(f"  {right}")
    print("  " + "-" * 32)
    for i in range(levels):
        bp = ob.bids[i].price if i < len(ob.bids) else None
        bv = ob.bids[i].volume if i < len(ob.bids) else None
        left = f"{bp:>8,} ({bv:>5,})" if bp else " " * 16
        print(f"  {left}")


if __name__ == "__main__":
    main()
