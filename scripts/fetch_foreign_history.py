"""fetch_foreign_history — KIS REST 종목별 일별 외인 순매수 historical fetch.

사용자 명시 (2026-05-15): 중기 (foreign_flow) backtest 활성을 위해 KIS REST
investor-trade-by-stock-daily 로 universe × N일 외인 순매수 데이터를 사전
fetch → sqlite 저장 → backtest 시 ForeignNetBuy event inject.

KIS 응답이 한 호출에 30 영업일 (output2 array) 반환 → 종목당 ceil(N/30) 호출.
순매수 KRW = frgn_ntby_qty (주식수) × stck_clpr (종가).

resumable: 이미 fetched (symbol, date) 쌍은 skip.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ks_ws.auth.token import get_token
from ks_ws.config import get_settings
from ks_ws.kis.http import make_client
from ks_ws.storage.universe import UniverseRegistry

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("ks_ws.kis.http").setLevel(logging.ERROR)
log = logging.getLogger("fetch_foreign_history")
log.setLevel(logging.INFO)


_DDL = """
CREATE TABLE IF NOT EXISTS foreign_flow (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,           -- YYYYMMDD
    net_buy_krw INTEGER NOT NULL,  -- frgn_ntby_qty × stck_clpr (외인 순매수 KRW)
    qty INTEGER NOT NULL,          -- frgn_ntby_qty (외인 순매수 주식수)
    close_price INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_ff_date ON foreign_flow(date);
"""

# 사용자 룰 5/18: 기관 column 추가 (orgn_ntby_qty). 기존 row 는 NULL.
_MIGRATIONS = [
    "ALTER TABLE foreign_flow ADD COLUMN inst_net_buy_krw INTEGER",
    "ALTER TABLE foreign_flow ADD COLUMN inst_qty INTEGER",
]

_PATH = "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily"
_TR_ID = "FHPTJ04160001"


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.executescript(_DDL)
    conn.execute("PRAGMA journal_mode=WAL")
    # 기관 column migrations — 이미 있으면 OperationalError 무시
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    return conn


def existing_pairs(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    rows = conn.execute("SELECT symbol, date FROM foreign_flow").fetchall()
    return {(r[0], r[1]) for r in rows}


def fetch_window(symbol: str, end_date_yyyymmdd: str, settings, token) -> list[tuple[str, str, int, int, int, int, int]]:
    """단일 호출 = end_date 부터 과거 30 영업일. 빈 list = 실패/no-data.

    Returns list of (symbol, date, frgn_net_krw, frgn_qty, close, inst_net_krw, inst_qty).
    """
    client = make_client(settings)
    try:
        resp = client.get(
            _PATH,
            headers={
                "authorization": f"Bearer {token}",
                "appkey": settings.app_key,
                "appsecret": settings.app_secret,
                "tr_id": _TR_ID,
                "tr_cont": "",
            },
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": end_date_yyyymmdd,
                "FID_ORG_ADJ_PRC": "",
                "FID_ETC_CLS_CODE": "",
            },
        )
        data = resp.json()
    except Exception as e:
        log.debug("fetch %s @%s: %s", symbol, end_date_yyyymmdd, e)
        return []
    finally:
        client.close()

    if data.get("rt_cd") != "0":
        return []
    rows = data.get("output2") or []
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        try:
            date = str(r.get("stck_bsop_date") or "").strip()
            clpr = int(r.get("stck_clpr") or 0)
            frgn_qty = int(r.get("frgn_ntby_qty") or 0)
            inst_qty = int(r.get("orgn_ntby_qty") or 0)
            if not date or clpr <= 0:
                continue
            frgn_net_krw = frgn_qty * clpr
            inst_net_krw = inst_qty * clpr
            out.append((symbol, date, frgn_net_krw, frgn_qty, clpr,
                        inst_net_krw, inst_qty))
        except (ValueError, TypeError):
            continue
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--top", type=int, default=0,
                   help="universe top N (default 0 = 전종목)")
    p.add_argument("--days", type=int, default=400,
                   help="과거 N영업일 (default 400, ~19개월). KIS 응답 30/call")
    p.add_argument("--workers", type=int, default=8,
                   help="동시 thread (default 8)")
    p.add_argument("--db", type=str, default="data/foreign_flow.sqlite")
    p.add_argument("--throttle-ms", type=int, default=100,
                   help="호출 사이 sleep (ms, default 100)")
    args = p.parse_args()

    settings = get_settings()
    token = get_token(settings)

    reg = UniverseRegistry("data/universe.sqlite")
    if args.top <= 0:
        universe = reg.top_by_market_cap(1_000_000)  # 사실상 전종목
    else:
        universe = reg.top_by_market_cap(args.top)
    reg.close()
    codes = [e.code for e in universe]

    # window endpoints — 영업일 N 씩 뒤로 walk
    # KIS mock 가 today/최근 1-2 영업일 데이터 안 주는 경우 빈번 → 5일 buffer.
    today = datetime.now(UTC).date() - timedelta(days=5)
    end_dates: list[str] = []
    business_days_per_call = 30
    n_calls_per_symbol = (args.days + business_days_per_call - 1) // business_days_per_call
    for i in range(n_calls_per_symbol):
        offset_days = i * 42  # 30 영업일 ≈ 42 calendar days
        end_dates.append((today - timedelta(days=offset_days)).strftime("%Y%m%d"))
    log.info("universe=%d days=%d calls/sym=%d total_calls=%d",
             len(codes), args.days, n_calls_per_symbol,
             len(codes) * n_calls_per_symbol)

    # Resumability: 이미 모든 일자 있는 종목은 skip 가능, 단순화: 일자 단위 check
    conn = open_db(args.db)
    already = existing_pairs(conn)
    log.info("existing rows: %d", len(already))

    # task list = (symbol, end_date)
    tasks: list[tuple[str, str]] = []
    for sym in codes:
        for end_date in end_dates:
            tasks.append((sym, end_date))
    total = len(tasks)

    completed = 0
    inserted = 0
    started = time.time()
    last_commit = started

    def _do(t: tuple[str, str]):
        sym, end_date = t
        if args.throttle_ms > 0:
            time.sleep(args.throttle_ms / 1000)
        return fetch_window(sym, end_date, settings, token)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_do, t): t for t in tasks}
        for fut in as_completed(futures):
            rows = fut.result()
            for sym, dt, frgn_net_krw, frgn_qty, clpr, inst_net_krw, inst_qty in rows:
                if (sym, dt) in already:
                    continue
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO foreign_flow "
                        "(symbol, date, net_buy_krw, qty, close_price, "
                        " inst_net_buy_krw, inst_qty, fetched_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (sym, dt, frgn_net_krw, frgn_qty, clpr,
                         inst_net_krw, inst_qty,
                         datetime.now(UTC).isoformat()),
                    )
                    already.add((sym, dt))
                    inserted += 1
                except Exception as e:
                    log.warning("insert %s %s: %s", sym, dt, e)
            completed += 1
            if completed % 50 == 0 or time.time() - last_commit > 5:
                conn.commit()
                last_commit = time.time()
                rate = completed / (time.time() - started)
                eta = (total - completed) / max(rate, 0.01) / 60
                log.info("call %d/%d (%.2f/s, ETA %.0fmin, rows ins=%d)",
                         completed, total, rate, eta, inserted)

    conn.commit()
    conn.close()
    elapsed = time.time() - started
    log.info("DONE — %d calls in %.0fs = %.2f/s, %d rows inserted",
             completed, elapsed, completed / max(elapsed, 0.01), inserted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
