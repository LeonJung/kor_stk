"""Re-fetch only the (symbol, end_time) pairs that failed with 500 in the
previous minute fetch (parsed from /tmp/fetch_minutes.log).

Strategy:
- Parse log for "! <symbol> @ <end_time> failed:" lines and FID_INPUT_ISCD URL params
- Build set of {(symbol, end_time)} that need retry
- For each, call fetch_minute_bars with throttle + 3 retries (exponential backoff)
- Append successful results to BarStore /1m/

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.fetch_minutes_retry --log /tmp/fetch_minutes.log
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


_FAIL_PAT = re.compile(
    r"FID_INPUT_ISCD=(?P<sym>\d+)&FID_INPUT_HOUR_1=(?P<et>\d+)"
)


def parse_failures(log_path: Path) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    text = log_path.read_text(encoding="utf-8", errors="replace")
    for m in _FAIL_PAT.finditer(text):
        out[m.group("sym")].add(m.group("et"))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default="/tmp/fetch_minutes.log")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0,
                        help="cap symbols (debug)")
    args = parser.parse_args()

    failures = parse_failures(Path(args.log))
    total_pairs = sum(len(v) for v in failures.values())
    print(f"=== Retry: {len(failures)} symbols, {total_pairs} (sym, end_time) pairs ===")

    from ks_ws.config import get_settings
    from ks_ws.market.kis_rest import fetch_minute_bars
    from ks_ws.storage.bars import BarStore

    settings = get_settings()
    bar_store = BarStore(args.data_dir)

    syms = list(failures.keys())
    if args.limit:
        syms = syms[: args.limit]

    started = time.monotonic()
    success_pairs = 0
    fail_pairs = 0
    total_bars = 0
    for j, sym in enumerate(syms):
        for et in sorted(failures[sym]):
            ok = False
            for attempt in range(args.max_retries):
                try:
                    bars = fetch_minute_bars(symbol=sym, end_time=et, settings=settings)
                    if bars:
                        bar_store.write(bars)
                        total_bars += len(bars)
                    ok = True
                    break
                except Exception:
                    time.sleep(0.5 * (2 ** attempt))  # 0.5, 1.0, 2.0s backoff
            if ok:
                success_pairs += 1
            else:
                fail_pairs += 1
        elapsed = time.monotonic() - started
        done = j + 1
        rate = done / max(elapsed, 0.01)
        eta = (len(syms) - done) / max(rate, 0.001)
        if done % 50 == 0 or done == len(syms):
            print(
                f"  [{done:>5d}/{len(syms)}] sym={sym} success_pairs={success_pairs} "
                f"fail={fail_pairs} bars={total_bars} rate={rate:.2f}/s eta={eta/60:.0f}min",
                flush=True,
            )
    print(f"  ✓ Retry done: {success_pairs} successes, {fail_pairs} still-failed, "
          f"{total_bars} new bars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
