"""trade_review_api.py — FastAPI backend for Vue.js trade review GUI.

사용법::

    PYTHONPATH=src .venv/bin/python -m scripts.trade_review_api
    # → http://localhost:8501/  = Vue SPA
    # → http://localhost:8501/api/...  = REST endpoints

Frontend: `scripts/trade_review_ui/index.html` (Vue 3 + plotly.js + axios via CDN).
Backend serves both static SPA and JSON API.

Endpoints:
- GET /api/modes
- GET /api/trades?mode=...&symbol=...&date=...
- GET /api/bars?symbol=...&date=YYYY-MM-DD
- GET /api/sector?symbol=...
- GET /api/stats?mode=...
- GET /api/next_trade?mode=...&symbol=...&date=...&uncertain_only=...
- POST /api/evaluate {mode, symbol, entry_ts, exit_ts, verdict, note}
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ks_ws.sources.sector import DEFAULT_KOSPI_TOP30_GICS, SectorClassifier
from ks_ws.storage.bars import BarStore
from ks_ws.storage.universe import UniverseRegistry

_KST = ZoneInfo("Asia/Seoul")
_DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
_REPORTS_ROOT = _DATA_ROOT / "reports"
_DB_PATH = _DATA_ROOT / "trade_evaluations.sqlite"
_UI_DIR = Path(__file__).resolve().parent / "trade_review_ui"

_MODE_INFO: dict[str, dict[str, str]] = {
    "V1": {
        "csv": "v1_trades.csv",
        "label": "V1 (개선 전 baseline)",
        "rule": (
            "vb baseline — Larry Williams 변동성돌파. 매일 시가 + k×(전일 H-L) "
            "trigger 가격 산출 (k=0.5). 분봉이 trigger 위로 cross 시 BUY. "
            "TP +2.5% / SL -1.5% / max_hold 240분 / same-day 1회 진입. "
            "종목 필터/symbol_weight/trailing 없음."
        ),
    },
    "V2": {
        "csv": "v2_trades.csv",
        "label": "V2 (Tier 1+3+5)",
        "rule": (
            "vb + Tier 1+3+5. Tier 1 = sector blacklist 60+ 종목 universe 필터. "
            "Tier 3 = anchor trailing (entry×1.015 도달 후 max_seen×0.99 이탈 시 청산). "
            "Tier 5 = SymbolWeightMatrix (walk-forward 60%→×3 / 50%→×2 / 40%→×1 / 차단)."
        ),
    },
    "V3": {
        "csv": "v3_trades.csv",
        "label": "V3 (vb_scalein 분할매매)",
        "rule": (
            "vb_scalein 분할매수/매도. 진입 50%/30%/20% (trigger / +0.5% / +1.0%). "
            "청산 33%/33%/잔여 (TP1 +2.0% / TP2 +3.0% / trailing). "
            "TP1 hit 시 SL→BE ratchet. ⚠️ round_close 버그 존재."
        ),
    },
    "V4": {
        "csv": "v4_trades.csv",
        "label": "V4 (blacklist 만, honest)",
        "rule": (
            "vb + Tier 1 blacklist 만 — fixed rule, no symbol_weight, no trailing. "
            "5/17 phase A/B/D 의 overfit 경고 검증용 honest baseline. "
            "결과: V1 -5.27M → V4 -3.87M (손실 27% 감소, 승률 거의 동일)."
        ),
    },
}
DEFAULT_BACKTEST_DIR = "vb_compare_20260517_4way"


# ---------- DB ----------


def _ensure_db() -> None:
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evaluation (
            mode TEXT NOT NULL,
            symbol TEXT NOT NULL,
            entry_ts TEXT NOT NULL,
            exit_ts TEXT NOT NULL,
            verdict TEXT NOT NULL,
            note TEXT DEFAULT '',
            evaluated_at TEXT NOT NULL,
            PRIMARY KEY (mode, symbol, entry_ts, exit_ts)
        )
        """
    )
    conn.commit()
    conn.close()


def _db() -> sqlite3.Connection:
    return sqlite3.connect(_DB_PATH)


# ---------- Cached data ----------

_bar_store_cache: BarStore | None = None
_sector_cache: SectorClassifier | None = None
_name_map_cache: dict[str, str] | None = None
_trades_cache: dict[tuple[str, str], pd.DataFrame] = {}


def _bar_store() -> BarStore:
    global _bar_store_cache
    if _bar_store_cache is None:
        _bar_store_cache = BarStore(str(_DATA_ROOT))
    return _bar_store_cache


def _sector_classifier() -> SectorClassifier:
    global _sector_cache
    if _sector_cache is None:
        _sector_cache = SectorClassifier(mapping=DEFAULT_KOSPI_TOP30_GICS)
    return _sector_cache


def _name_map() -> dict[str, str]:
    global _name_map_cache
    if _name_map_cache is None:
        try:
            reg = UniverseRegistry(str(_DATA_ROOT / "universe.sqlite"))
            entries = reg.top_by_market_cap(100_000)
            reg.close()
            _name_map_cache = {e.code: e.name for e in entries}
        except Exception:
            _name_map_cache = {}
    return _name_map_cache


def _load_trades(backtest_dir: str, mode: str) -> pd.DataFrame:
    key = (backtest_dir, mode)
    if key in _trades_cache:
        return _trades_cache[key]
    csv = _MODE_INFO[mode]["csv"]
    path = _REPORTS_ROOT / backtest_dir / csv
    if not path.exists():
        df = pd.DataFrame()
    else:
        df = pd.read_csv(path, dtype={"symbol": str})
        df["symbol"] = df["symbol"].str.zfill(6)
        # entry_ts / exit_ts 모두 KST ISO 로 일관 변환 (CSV 에 UTC/KST 섞여있음)
        df["entry_dt"] = pd.to_datetime(df["entry_ts"], utc=True)
        df["exit_dt"] = pd.to_datetime(df["exit_ts"], utc=True)
        df["entry_ts"] = df["entry_dt"].dt.tz_convert(_KST).apply(
            lambda d: d.isoformat()
        )
        df["exit_ts"] = df["exit_dt"].dt.tz_convert(_KST).apply(
            lambda d: d.isoformat()
        )
        df["entry_date"] = (
            df["entry_dt"].dt.tz_convert(_KST).dt.strftime("%Y-%m-%d")
        )
    _trades_cache[key] = df
    return df


def _is_uncertain(row: dict) -> bool:
    pct = float(row["pnl_pct"])
    hold = int(row["hold_minutes"])
    if hold < 30 and float(row["pnl_krw"]) < 0:
        return True
    return pct < -2.0 or pct > 5.0


# ---------- FastAPI ----------

app = FastAPI(title="ks_ws Trade Review API")


class EvaluateIn(BaseModel):
    backtest_dir: str
    mode: str
    symbol: str
    entry_ts: str
    exit_ts: str
    verdict: str  # good / bad / unsure
    note: str = ""


@app.on_event("startup")
def _startup() -> None:
    _ensure_db()


@app.get("/api/modes")
def list_modes() -> dict:
    return {
        "modes": [
            {"id": k, "label": v["label"], "rule": v["rule"]}
            for k, v in _MODE_INFO.items()
        ],
        "default_mode": "V2",
        "backtest_dirs": sorted(
            p.name for p in _REPORTS_ROOT.iterdir()
            if p.is_dir() and (p / "v1_trades.csv").exists()
        ),
        "default_backtest_dir": DEFAULT_BACKTEST_DIR,
    }


@app.get("/api/symbols")
def list_symbols(backtest_dir: str, mode: str) -> dict:
    df = _load_trades(backtest_dir, mode)
    if df.empty:
        return {"symbols": []}
    counts = df["symbol"].value_counts().to_dict()
    nm = _name_map()
    # 종목번호 ascending 으로 정렬 (사용자 명시 2026-05-17)
    out = sorted(
        [
            {"symbol": s, "name": nm.get(s, "?"), "count": int(c)}
            for s, c in counts.items()
        ],
        key=lambda x: x["symbol"],
    )
    return {"symbols": out}


@app.get("/api/dates")
def list_dates(backtest_dir: str, mode: str, symbol: str | None = None) -> dict:
    df = _load_trades(backtest_dir, mode)
    if symbol:
        df = df[df["symbol"] == symbol]
    if df.empty:
        return {"dates": []}
    return {"dates": sorted(df["entry_date"].unique().tolist(), reverse=True)}


@app.get("/api/stats")
def stats(backtest_dir: str, mode: str) -> dict:
    df = _load_trades(backtest_dir, mode)
    conn = _db()
    cur = conn.execute(
        "SELECT verdict, COUNT(*) FROM evaluation WHERE mode=? GROUP BY verdict",
        (mode,),
    )
    counts = {v: c for v, c in cur.fetchall()}
    conn.close()
    return {
        "total_trades": int(len(df)),
        "good": counts.get("good", 0),
        "bad": counts.get("bad", 0),
        "unsure": counts.get("unsure", 0),
        "total_evaluated": sum(counts.values()),
    }


def _evaluation_set(mode: str) -> set[tuple[str, str, str]]:
    conn = _db()
    cur = conn.execute(
        "SELECT symbol, entry_ts, exit_ts FROM evaluation WHERE mode=?", (mode,),
    )
    out = {(s, e, x) for s, e, x in cur.fetchall()}
    conn.close()
    return out


@app.get("/api/next_trade")
def next_trade(
    backtest_dir: str, mode: str, symbol: str | None = None,
    date: str | None = None, uncertain_only: bool = False,
) -> dict:
    df = _load_trades(backtest_dir, mode)
    if df.empty:
        raise HTTPException(404, "no trades")
    if symbol:
        df = df[df["symbol"] == symbol]
    if date:
        df = df[df["entry_date"] == date]
    if df.empty:
        return {"trade": None, "remaining": 0, "matched": 0}
    rows = df.to_dict("records")
    if uncertain_only:
        rows = [r for r in rows if _is_uncertain(r)]
    evaluated = _evaluation_set(mode)
    matched = len(rows)
    next_row = None
    remaining = 0
    for r in rows:
        key = (r["symbol"], r["entry_ts"], r["exit_ts"])
        if key not in evaluated:
            remaining += 1
            if next_row is None:
                next_row = r
    if next_row is None:
        return {"trade": None, "remaining": 0, "matched": matched}
    nm = _name_map()
    sector = _sector_classifier().classify(next_row["symbol"])
    rule = _MODE_INFO[mode]["rule"]
    return {
        "trade": {
            "symbol": next_row["symbol"],
            "name": nm.get(next_row["symbol"], "?"),
            "sector": sector,
            "entry_ts": next_row["entry_ts"],
            "entry_date": next_row["entry_date"],
            "exit_ts": next_row["exit_ts"],
            "entry_price": int(next_row["entry_price"]),
            "exit_price": int(next_row["exit_price"]),
            "pnl_krw": int(next_row["pnl_krw"]),
            "pnl_pct": float(next_row["pnl_pct"]),
            "hold_minutes": int(next_row["hold_minutes"]),
            "is_uncertain": _is_uncertain(next_row),
            "rule": rule,
        },
        "remaining": remaining,
        "matched": matched,
    }


@app.get("/api/trades_list")
def trades_list(
    backtest_dir: str, mode: str, symbol: str | None = None,
    date: str | None = None,
) -> dict:
    """필터된 trade 들의 시간순 list. 회차 선택용."""
    df = _load_trades(backtest_dir, mode)
    if symbol:
        df = df[df["symbol"] == symbol]
    if date:
        df = df[df["entry_date"] == date]
    if df.empty:
        return {"trades": []}
    df = df.sort_values("entry_ts")
    evaluated = _evaluation_set(mode)
    nm = _name_map()
    sector = _sector_classifier()
    rule = _MODE_INFO[mode]["rule"]
    out = []
    for _, r in df.iterrows():
        key = (r["symbol"], r["entry_ts"], r["exit_ts"])
        out.append({
            "symbol": r["symbol"],
            "name": nm.get(r["symbol"], "?"),
            "sector": sector.classify(r["symbol"]),
            "entry_ts": r["entry_ts"],
            "entry_date": r["entry_date"],
            "exit_ts": r["exit_ts"],
            "entry_price": int(r["entry_price"]),
            "exit_price": int(r["exit_price"]),
            "pnl_krw": int(r["pnl_krw"]),
            "pnl_pct": float(r["pnl_pct"]),
            "hold_minutes": int(r["hold_minutes"]),
            "is_uncertain": _is_uncertain(r),
            "evaluated": key in evaluated,
            "rule": rule,
        })
    return {"trades": out}


@app.get("/api/bars")
def get_bars(symbol: str, date: str) -> dict:
    """종목별 parquet schema 가 다름 (KST tz-aware vs UTC naive). epoch_ms
    비교로 둘 다 처리. 응답 timestamp 는 KST ISO 일관."""
    import duckdb
    from datetime import timezone as _tz
    kst_date = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=_KST)
    start_kst = kst_date.replace(hour=8, minute=0)
    end_kst = kst_date.replace(hour=16, minute=0)
    start_ms = int(start_kst.astimezone(_tz.utc).timestamp() * 1000)
    end_ms = int(end_kst.astimezone(_tz.utc).timestamp() * 1000)
    bucket = _DATA_ROOT / "bars" / "1m" / symbol
    if not bucket.exists():
        return {"bars": [], "count": 0}
    glob = str(bucket / "*.parquet")
    con = duckdb.connect(":memory:")
    try:
        # value 컬럼이 누적 거래대금이라 LAG 으로 분봉당 diff 계산
        rows = con.execute(
            f"SELECT timestamp, epoch_ms(timestamp) AS ems, "
            f"open, high, low, close, volume, value, "
            f"COALESCE(LAG(value) OVER (ORDER BY timestamp), 0) AS prev_value "
            f"FROM read_parquet('{glob}') "
            f"WHERE epoch_ms(timestamp) >= ? AND epoch_ms(timestamp) < ? "
            f"ORDER BY ems",
            [start_ms, end_ms],
        ).fetchall()
    finally:
        con.close()
    out: list[dict[str, Any]] = []
    for ts, ems, o, h, l, c, v, val, prev_val in rows:
        kst_ts = datetime.fromtimestamp(ems / 1000, tz=_tz.utc).astimezone(_KST)
        # 분봉당 거래대금 = 누적 - 직전 누적. 음수 (자료 결손) 시 fallback.
        bar_value = (val or 0) - (prev_val or 0)
        if bar_value < 0:
            bar_value = int(c) * int(v)
        out.append({
            "timestamp": kst_ts.isoformat(),
            "open": int(o), "high": int(h), "low": int(l), "close": int(c),
            "volume": int(v),
            "value": int(bar_value),
        })
    # 전일 일봉 close — hover 시 % 계산용
    prev_close_val: int | None = None
    day_bucket = _DATA_ROOT / "bars" / "1d" / symbol
    if day_bucket.exists():
        con2 = duckdb.connect(":memory:")
        try:
            day_glob = str(day_bucket / "*.parquet")
            r = con2.execute(
                f"SELECT close FROM read_parquet('{day_glob}') "
                f"WHERE CAST(timestamp AS DATE) < ? "
                f"ORDER BY timestamp DESC LIMIT 1",
                [date],
            ).fetchone()
            if r:
                prev_close_val = int(r[0])
        finally:
            con2.close()
    return {"bars": out, "count": len(out), "prev_close": prev_close_val}


@app.get("/api/daily_bars")
def get_daily_bars(symbol: str, date: str, window: int = 10) -> dict:
    """trade date 기준 ±window 거래일 일봉. trade_idx = 매매일의 array index."""
    import duckdb
    bucket = _DATA_ROOT / "bars" / "1d" / symbol
    if not bucket.exists():
        return {"bars": [], "count": 0, "trade_idx": -1}
    glob = str(bucket / "*.parquet")
    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(
            f"SELECT timestamp, open, high, low, close, volume, value "
            f"FROM read_parquet('{glob}') ORDER BY timestamp"
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return {"bars": [], "count": 0, "trade_idx": -1}
    target_date = datetime.strptime(date, "%Y-%m-%d").date()
    # trade date 와 정확히 일치, 없으면 그 이전 마지막
    idx = -1
    for i, r in enumerate(rows):
        d = r[0].date() if hasattr(r[0], "date") else r[0]
        if d == target_date:
            idx = i; break
        if d < target_date:
            idx = i
    if idx < 0:
        return {"bars": [], "count": 0, "trade_idx": -1}
    lo = max(0, idx - window)
    hi = min(len(rows), idx + window + 1)
    out: list[dict[str, Any]] = []
    for ts, o, h, l, c, v, val in rows[lo:hi]:
        kst_ts = ts.astimezone(_KST) if hasattr(ts, "tzinfo") and ts.tzinfo else ts
        out.append({
            "timestamp": kst_ts.isoformat() if hasattr(kst_ts, "isoformat") else str(kst_ts),
            "open": int(o), "high": int(h), "low": int(l), "close": int(c),
            "volume": int(v), "value": int(val or 0),
        })
    return {"bars": out, "count": len(out), "trade_idx": idx - lo}


@app.get("/api/sector")
def get_sector(symbol: str) -> dict:
    return {"sector": _sector_classifier().classify(symbol)}


@app.post("/api/evaluate")
def evaluate(ev: EvaluateIn) -> dict:
    if ev.verdict not in ("good", "bad", "unsure"):
        raise HTTPException(400, "verdict must be good/bad/unsure")
    if ev.mode not in _MODE_INFO:
        raise HTTPException(400, f"unknown mode {ev.mode}")
    conn = _db()
    conn.execute(
        "INSERT OR REPLACE INTO evaluation "
        "(mode, symbol, entry_ts, exit_ts, verdict, note, evaluated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ev.mode, ev.symbol, ev.entry_ts, ev.exit_ts, ev.verdict, ev.note,
         datetime.now(_KST).isoformat()),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------- Static UI (Vue 3 SPA) ----------


@app.get("/")
def root() -> FileResponse:
    return FileResponse(_UI_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(_UI_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8501)
