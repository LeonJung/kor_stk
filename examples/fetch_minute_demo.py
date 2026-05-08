"""Fetch the most recent 1-minute bars for a symbol.

Run (during or after KRX market hours):
    uv run examples/fetch_minute_demo.py [SYMBOL] [HHMMSS]
"""

import sys

from ks_ws.market.kis_rest import fetch_minute_bars


def main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "005930"
    end_time = sys.argv[2] if len(sys.argv) > 2 else "153000"

    bars = fetch_minute_bars(symbol, end_time=end_time)
    print(f"=== Minute bars ({symbol}, end={end_time}) ===")
    print(f"  got {len(bars)} bars")
    if not bars:
        return
    print(f"  {'time':<19} {'open':>8} {'high':>8} {'low':>8} {'close':>8} {'volume':>10}")
    for b in bars:
        ts = b.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {ts:<19} {b.open:>8,} {b.high:>8,} {b.low:>8,} {b.close:>8,} {b.volume:>10,}")


if __name__ == "__main__":
    main()
