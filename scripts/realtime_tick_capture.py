"""Realtime tick capture launcher — 평일 (08:00-15:30 KST) WS H0STCNT0
구독 → tick SQLite append + 1초 aggregate Parquet.

용도: 사용자 요청 (2026-05-10) — 5/11 (월) ~ 5/12 (화) 이틀만 실시간으로
초봉/틱봉 데이터 받기. 08:00 부터 시작 (정규장 전 호가 접수 시간 + 장 시작
호가 모집 + 시간외 단일가 일부 포함).

동작:
- KIS WS 가 동시 구독 max 20 (H0STCNT0 + H0STASP0 합쳐 20). 본 launcher 는
  H0STCNT0 (체결) 만 사용 → 20 종목 동시 구독 한도.
- universe = 시총 상위 N (default 20). 더 받으려면 process 여러개 분할.
- 5/11 08:00 자동 start, 5/12 15:30 자동 stop. 그 사이 휴장이면 skip.
- 08:00-09:00 = 정규장 전 호가 접수 / 시간외 단일가, 09:00-15:30 = 정규장.
- 평일 장중/장전 외에는 60초 sleep.

Rate limit 극복 (사용자 요청 2026-05-10):
- WS 자체는 push 라 rate limit 무관. 단 동시 구독 20 종목 한도 → multi-process
  또는 multi-account 로 확장 가능.
- 부가 REST polling (분봉 1회/분, 외국인 일별 1회/일, 거래대금 ranking 30초)
  은 ``RateLimiter`` 로 자동 throttle (live=15 req/s default).
- 500/429 자동 retry: _RetryTransport 가 exponential backoff (0.5/1/2s) ×3.
- multi-account 분산: settings.app_key prefix 별 rate limiter 별도 → 같은 process
  에서 여러 KIS key 사용 시 rate 합산 X.

저장:
- ticks: SQLite ``data/ticks.sqlite`` (symbol, ts_iso, price, volume, aggressor)
- 1초 봉 aggregate: 매분 마감 시 자체 OHLC 계산 → BarStore '1s' Parquet
  (V1 = SQLite 만, aggregate 는 별도 batch 권장)

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.realtime_tick_capture \
        --start 2026-05-11 --end 2026-05-12 --top-n 20

휴장일에는 자동 sleep, 강제 종료는 Ctrl+C.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

_KST = ZoneInfo("Asia/Seoul")

_TICK_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    ts_iso TEXT NOT NULL,
    price INTEGER NOT NULL,
    volume INTEGER NOT NULL,
    aggressor TEXT
);
CREATE INDEX IF NOT EXISTS idx_ticks_symbol_ts ON ticks(symbol, ts_iso);
"""


def open_tick_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_TICK_SCHEMA)
    conn.commit()
    return conn


def kst_today() -> date:
    return datetime.now(UTC).astimezone(_KST).date()


def kst_now() -> datetime:
    return datetime.now(UTC).astimezone(_KST)


async def capture_session(symbols: list[str], conn: sqlite3.Connection,
                          stop_at_kst: datetime) -> int:
    """Open WS, subscribe symbols, append every tick to SQLite. Stop at
    ``stop_at_kst``. Return total tick count."""
    from ks_ws.config import get_settings
    from ks_ws.kis.realtime import KisRealtimeFeed

    settings = get_settings()
    count = 0
    insert_sql = (
        "INSERT INTO ticks (symbol, ts_iso, price, volume, aggressor) "
        "VALUES (?, ?, ?, ?, ?)"
    )
    today = kst_now().date().isoformat()
    async with KisRealtimeFeed(settings) as feed:
        for sym in symbols:
            await feed.subscribe("H0STCNT0", sym)
        async for frame in feed:
            if kst_now() >= stop_at_kst:
                break
            try:
                tr_id, _enc, records = KisRealtimeFeed.parse_frame(frame)
                if tr_id != "H0STCNT0":
                    continue
                # H0STCNT0 record fields (caret-split):
                # [0]종목코드 [1]체결시각HHMMSS [2]현재가 ... [12]체결거래량
                for rec in records:
                    if len(rec) < 13:
                        continue
                    sym = rec[0]
                    hhmmss = rec[1]
                    if len(hhmmss) != 6:
                        continue
                    ts_iso = (
                        f"{today}T{hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}+09:00"
                    )
                    try:
                        price = int(rec[2])
                        volume = int(rec[12])
                    except (ValueError, IndexError):
                        continue
                    conn.execute(insert_sql, (sym, ts_iso, price, volume, None))
                    count += 1
                if count > 0 and count % 500 == 0:
                    conn.commit()
                    print(f"  [{kst_now():%H:%M:%S}] ticks={count}", flush=True)
            except Exception as e:
                print(f"  ! tick parse error: {e}", flush=True)
                continue
    conn.commit()
    return count


def aggregate_ticks_to_seconds(conn: sqlite3.Connection, day: date,
                                bar_store_root: Path) -> int:
    """Aggregate ticks into 1-second OHLCV bars and write to BarStore."""
    from ks_ws.domain import Bar
    from ks_ws.storage.bars import BarStore

    bar_store = BarStore(bar_store_root)
    cur = conn.execute(
        """
        SELECT symbol, substr(ts_iso, 1, 19) AS sec_iso,
               MIN(price) AS lo, MAX(price) AS hi,
               (SELECT price FROM ticks WHERE symbol = t.symbol
                AND substr(ts_iso, 1, 19) = substr(t.ts_iso, 1, 19)
                ORDER BY id ASC LIMIT 1) AS open_price,
               (SELECT price FROM ticks WHERE symbol = t.symbol
                AND substr(ts_iso, 1, 19) = substr(t.ts_iso, 1, 19)
                ORDER BY id DESC LIMIT 1) AS close_price,
               SUM(volume) AS vol,
               SUM(price * volume) AS val
        FROM ticks t
        WHERE substr(ts_iso, 1, 10) = ?
        GROUP BY symbol, sec_iso
        ORDER BY symbol, sec_iso
        """,
        (day.isoformat(),),
    )
    bars: list[Bar] = []
    for row in cur.fetchall():
        symbol, sec_iso, lo, hi, op, cl, vol, val = row
        try:
            ts = datetime.fromisoformat(sec_iso).replace(tzinfo=UTC)
            bars.append(
                Bar(
                    symbol=symbol, timestamp=ts, timeframe="1s",
                    open=int(op), high=int(hi), low=int(lo), close=int(cl),
                    volume=int(vol), value=int(val),
                )
            )
        except Exception:
            continue
    written = bar_store.write(bars) if bars else 0
    return written


async def main_async(args) -> int:
    from ks_ws.calendar import is_market_open, is_trading_day
    from ks_ws.storage.universe import UniverseRegistry

    reg = UniverseRegistry(args.universe_db)
    universe = reg.top_by_market_cap(args.top_n)
    symbols = [e.code for e in universe]
    reg.close()
    print(f"=== Realtime tick capture ===")
    print(f"  Window: {args.start} ~ {args.end}")
    print(f"  Symbols: {len(symbols)} (top {args.top_n} by market cap)")
    print(f"  Tick DB: {args.tick_db}")

    conn = open_tick_db(Path(args.tick_db))

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)
    cur_date = start_date
    while cur_date <= end_date:
        if not is_trading_day(cur_date):
            print(f"  {cur_date} non-trading, skipping")
            cur_date += timedelta(days=1)
            continue
        # Wait until 08:00 KST of cur_date (정규장 전 호가 접수 시간 포함)
        target_open = datetime.combine(cur_date, datetime.min.time()).replace(
            hour=8, minute=0, tzinfo=_KST
        )
        target_close = target_open.replace(hour=15, minute=30)
        while kst_now() < target_open:
            await asyncio.sleep(60)
            if kst_now().date() > cur_date:
                break
        if kst_now().date() != cur_date:
            cur_date += timedelta(days=1)
            continue
        print(f"  --- {cur_date} 08:00-15:30 KST capturing ---")
        n = await capture_session(symbols, conn, stop_at_kst=target_close)
        print(f"  ✓ {cur_date} captured {n} ticks")
        # Aggregate → 1s bars
        written = aggregate_ticks_to_seconds(conn, cur_date, Path(args.data_dir))
        print(f"  ✓ {cur_date} 1s bars written: {written}")
        cur_date += timedelta(days=1)
    conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-05-11", help="YYYY-MM-DD")
    parser.add_argument("--end", default="2026-05-12", help="YYYY-MM-DD")
    parser.add_argument("--top-n", type=int, default=20,
                        help="N symbols to subscribe (KIS WS max 20 per process)")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--universe-db", default="data/universe.sqlite")
    parser.add_argument("--tick-db", default="data/ticks.sqlite")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
