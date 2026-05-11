"""Fetch 2-week historical data for all KRX universe (excluding ETF/ETN/ELW).

What gets fetched:
- daily bars (모든 종목, FinanceDataReader 빠름) → BarStore /1d/
- minute bars (top 200 by market cap, KIS REST) → BarStore /1m/  -- 실행 시간 길어
  → 분봉은 separate flag 로만 enable (default daily 만)

Progress: per-symbol chunks 100 단위로 진행상황 print, BarStore 에 즉시 저장.

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.fetch_2week_data --days 14
    PYTHONPATH=src .venv/bin/python -m scripts.fetch_2week_data --days 14 --minutes  # 분봉 추가
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

# allow `scripts.` import via absolute path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def fetch_daily_bars_via_fdr(codes: list[str], start: date, end: date) -> dict[str, list]:
    """Per-code daily bar fetch via FinanceDataReader. Returns {code: list[Bar]}."""
    import FinanceDataReader as fdr  # noqa: PLC0415
    from ks_ws.domain import Bar

    out: dict[str, list[Bar]] = {}
    for code in codes:
        try:
            df = fdr.DataReader(code, start, end)
        except Exception as e:
            print(f"  ! {code} fetch failed: {e}", flush=True)
            continue
        if df is None or len(df) == 0:
            continue
        bars: list[Bar] = []
        for ts, row in df.iterrows():
            try:
                ts_utc = datetime(ts.year, ts.month, ts.day, tzinfo=UTC)
                bars.append(
                    Bar(
                        symbol=code,
                        timestamp=ts_utc,
                        timeframe="1d",
                        open=int(row["Open"]),
                        high=int(row["High"]),
                        low=int(row["Low"]),
                        close=int(row["Close"]),
                        volume=int(row["Volume"]),
                        value=int(row["Volume"] * row["Close"]),  # FDR 가 Amount 없으면 추정
                    )
                )
            except Exception:
                continue
        if bars:
            out[code] = bars
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14, help="how many days back to fetch")
    parser.add_argument("--minutes", action="store_true", help="also fetch minute bars (top N)")
    parser.add_argument("--top-n-minutes", type=int, default=999999, help="N for minute fetch (default = ALL)")
    parser.add_argument("--data-dir", default="data", help="bar store root")
    parser.add_argument("--universe-db", default="data/universe.sqlite")
    parser.add_argument("--limit", type=int, default=0, help="limit symbols (debug)")
    parser.add_argument(
        "--include-preferred", action="store_true",
        help="include preferred stocks (default exclude — H strategy needs them)"
    )
    args = parser.parse_args()

    from ks_ws.storage.bars import BarStore
    from ks_ws.storage.universe import UniverseRegistry

    end = date.today() + timedelta(days=1)  # FDR end 는 exclusive 처럼 동작 — +1
    start = end - timedelta(days=args.days + 7)  # 주말 보정

    universe = UniverseRegistry(args.universe_db)
    entries = universe.all(
        markets=("KOSPI", "KOSDAQ"),
        exclude_preferred=not args.include_preferred,
        exclude_spac=True,
    )
    if args.limit:
        entries = entries[: args.limit]
    codes = [e.code for e in entries]
    print(f"=== Universe: {len(codes)} symbols (KOSPI+KOSDAQ, ETF/ETN/ELW excluded, SPAC excluded)")
    print(f"  Date range: {start} ~ {end}")

    bar_store = BarStore(args.data_dir)

    # ----- Daily bars (FDR, fast) -----------------------------------------
    print(f"\n--- Fetching daily bars (FinanceDataReader) ---")
    chunk = 50
    total_bars = 0
    started = time.monotonic()
    for i in range(0, len(codes), chunk):
        batch = codes[i : i + chunk]
        result = fetch_daily_bars_via_fdr(batch, start, end)
        if result:
            flat = [b for bars in result.values() for b in bars]
            written = bar_store.write(flat)
            total_bars += written
        elapsed = time.monotonic() - started
        rate = (i + len(batch)) / max(elapsed, 0.01)
        eta = (len(codes) - i - len(batch)) / max(rate, 0.01)
        print(
            f"  [{i + len(batch):>5d}/{len(codes)}] daily bars written={total_bars}  "
            f"rate={rate:.1f} sym/s  eta={eta:.0f}s",
            flush=True,
        )
    print(f"  ✓ Daily bars complete: {total_bars} rows written")

    # ----- Minute bars (KIS REST, slower) --------------------------------
    if args.minutes:
        # KIS inquire-time-itemchartprice 는 가장 최근 거래일 한정 + 30bars/호출.
        # 1일치 = 13 호출 (15:30, 15:00, 14:30, ..., 09:30). 종목 전체 = 5시간 (mock).
        print(f"\n--- Fetching minute bars (KIS REST, 1일치 paginate) for "
              f"top {args.top_n_minutes if args.top_n_minutes < 999999 else 'ALL'} ---")
        if args.top_n_minutes >= 999999:
            active = universe.all(markets=("KOSPI", "KOSDAQ"),
                                  exclude_preferred=False, exclude_spac=True)
        else:
            active = universe.top_by_market_cap(args.top_n_minutes)
        try:
            from ks_ws.config import get_settings
            from ks_ws.market.kis_rest import fetch_minute_bars
            settings = get_settings()
        except Exception as e:
            print(f"  KIS settings unavailable: {e}; skipping minute fetch")
            return 0
        # 13 end_time 들 (15:30 부터 09:30 까지 30분 단위)
        end_times = [
            f"{h:02d}{m:02d}00"
            for h, m in [
                (15, 30), (15, 0), (14, 30), (14, 0), (13, 30), (13, 0),
                (12, 30), (12, 0), (11, 30), (11, 0), (10, 30), (10, 0),
                (9, 30),
            ]
        ]
        total_min_bars = 0
        started = time.monotonic()
        for j, e in enumerate(active):
            symbol_total = 0
            for et in end_times:
                try:
                    bars = fetch_minute_bars(symbol=e.code, end_time=et, settings=settings)
                    if bars:
                        bar_store.write(bars)
                        symbol_total += len(bars)
                except Exception as exc:
                    print(f"  ! {e.code} @ {et} failed: {exc}", flush=True)
                    continue
            total_min_bars += symbol_total
            elapsed = time.monotonic() - started
            done = j + 1
            rate = done / max(elapsed, 0.01)
            eta = (len(active) - done) / max(rate, 0.001)
            if done % 5 == 0 or done == len(active):
                print(
                    f"  [{done:>5d}/{len(active)}] {e.code} +{symbol_total}  "
                    f"total={total_min_bars}  rate={rate:.2f} sym/s  eta={eta/60:.0f}min",
                    flush=True,
                )
        print(f"  ✓ Minute bars complete: {total_min_bars} rows")

    universe.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
