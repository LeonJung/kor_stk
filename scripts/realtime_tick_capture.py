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

# SAFETY GUARD: 호가 (orderbook) capture cutoff. 사용자 명시 (2026-05-12):
# 5/12 + 5/13 이틀만 호가 누적, 그 이후 자동 skip. 더 누적하려면 이 값 수정.
_ORDERBOOK_CAPTURE_END_KST = date(2026, 5, 13)

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

CREATE TABLE IF NOT EXISTS orderbook (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    ts_iso TEXT NOT NULL,
    ask_px_1 INTEGER, ask_qty_1 INTEGER,
    ask_px_2 INTEGER, ask_qty_2 INTEGER,
    ask_px_3 INTEGER, ask_qty_3 INTEGER,
    ask_px_4 INTEGER, ask_qty_4 INTEGER,
    ask_px_5 INTEGER, ask_qty_5 INTEGER,
    bid_px_1 INTEGER, bid_qty_1 INTEGER,
    bid_px_2 INTEGER, bid_qty_2 INTEGER,
    bid_px_3 INTEGER, bid_qty_3 INTEGER,
    bid_px_4 INTEGER, bid_qty_4 INTEGER,
    bid_px_5 INTEGER, bid_qty_5 INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ob_symbol_ts ON orderbook(symbol, ts_iso);
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
    tick_count = 0
    ob_count = 0
    _ob_cutoff_warned = False
    insert_tick = (
        "INSERT INTO ticks (symbol, ts_iso, price, volume, aggressor) "
        "VALUES (?, ?, ?, ?, ?)"
    )
    insert_ob = (
        "INSERT INTO orderbook (symbol, ts_iso, "
        "ask_px_1, ask_qty_1, ask_px_2, ask_qty_2, ask_px_3, ask_qty_3, "
        "ask_px_4, ask_qty_4, ask_px_5, ask_qty_5, "
        "bid_px_1, bid_qty_1, bid_px_2, bid_qty_2, bid_px_3, bid_qty_3, "
        "bid_px_4, bid_qty_4, bid_px_5, bid_qty_5"
        ") VALUES (?,?, ?,?, ?,?, ?,?, ?,?, ?,?, ?,?, ?,?, ?,?, ?,?, ?,?)"
    )
    today = kst_now().date().isoformat()
    async with KisRealtimeFeed(settings) as feed:
        for sym in symbols:
            await feed.subscribe("H0STCNT0", sym)
            await feed.subscribe("H0STASP0", sym)
        async for frame in feed:
            if kst_now() >= stop_at_kst:
                break
            try:
                tr_id, _enc, records = KisRealtimeFeed.parse_frame(frame)
                if tr_id == "H0STCNT0":
                    # H0STCNT0: [0]종목 [1]체결시각HHMMSS [2]현재가 ... [12]체결거래량
                    for rec in records:
                        if len(rec) < 13:
                            continue
                        hhmmss = rec[1]
                        if len(hhmmss) != 6:
                            continue
                        ts_iso = f"{today}T{hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}+09:00"
                        try:
                            price = int(rec[2])
                            volume = int(rec[12])
                        except (ValueError, IndexError):
                            continue
                        conn.execute(insert_tick, (rec[0], ts_iso, price, volume, None))
                        tick_count += 1
                elif tr_id == "H0STASP0":
                    # SAFETY: cutoff 지난 경우 호가 insert skip
                    _today = kst_now().date()
                    if _today > _ORDERBOOK_CAPTURE_END_KST:
                        if not _ob_cutoff_warned:
                            print(f"  ORDERBOOK CAPTURE STOPPED: today {_today} > cutoff {_ORDERBOOK_CAPTURE_END_KST}", flush=True)
                            _ob_cutoff_warned = True
                        continue
                    # H0STASP0: [0]종목 [1]영업시각 [2]호가시각HHMMSS
                    # [3..12]매도호가1~10  [13..22]매수호가1~10
                    # [23..32]매도잔량1~10 [33..42]매수잔량1~10
                    for rec in records:
                        if len(rec) < 43:
                            continue
                        hhmmss = rec[2]
                        if len(hhmmss) != 6:
                            continue
                        ts_iso = f"{today}T{hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}+09:00"
                        try:
                            ask_px = [int(rec[3 + i]) for i in range(5)]
                            bid_px = [int(rec[13 + i]) for i in range(5)]
                            ask_qty = [int(rec[23 + i]) for i in range(5)]
                            bid_qty = [int(rec[33 + i]) for i in range(5)]
                        except (ValueError, IndexError):
                            continue
                        conn.execute(insert_ob, (
                            rec[0], ts_iso,
                            ask_px[0], ask_qty[0], ask_px[1], ask_qty[1],
                            ask_px[2], ask_qty[2], ask_px[3], ask_qty[3],
                            ask_px[4], ask_qty[4],
                            bid_px[0], bid_qty[0], bid_px[1], bid_qty[1],
                            bid_px[2], bid_qty[2], bid_px[3], bid_qty[3],
                            bid_px[4], bid_qty[4],
                        ))
                        ob_count += 1
                else:
                    continue
                if (tick_count + ob_count) > 0 and (tick_count + ob_count) % 1000 == 0:
                    conn.commit()
                    print(f"  [{kst_now():%H:%M:%S}] ticks={tick_count} ob={ob_count}", flush=True)
            except Exception as e:
                print(f"  ! parse error: {e}", flush=True)
                continue
    conn.commit()
    return tick_count + ob_count


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
