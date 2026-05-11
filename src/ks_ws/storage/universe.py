"""UniverseRegistry — KRX KOSPI + KOSDAQ 보통주/우선주 list (ETF/ETN/ELW 제외)
+ 메타 (시총, 시장구분, ISIN). 매일 1회 갱신.

source: FinanceDataReader (KRX 직접 scrape, 무인증). 향후 KIS API 로 교체 가능.

ETF/ETN/ELW 는 fdr.StockListing('KOSPI'/'KOSDAQ') 가 이미 제외 (별도
StockListing('ETF/KR')). 우선주는 포함 (H 우선주페어 strategy 사용).

SPAC (스팩) 은 종목명에 '스팩' 포함 → 자동 제외.
"""

import json
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS universe (
    code TEXT PRIMARY KEY,
    isin TEXT,
    name TEXT NOT NULL,
    market TEXT NOT NULL,
    is_preferred INTEGER NOT NULL DEFAULT 0,
    is_spac INTEGER NOT NULL DEFAULT 0,
    listed_shares INTEGER,
    market_cap_krw INTEGER,
    last_close_krw INTEGER,
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_universe_market ON universe(market);
CREATE INDEX IF NOT EXISTS idx_universe_market_cap ON universe(market_cap_krw DESC);
"""


@dataclass(frozen=True)
class UniverseEntry:
    code: str
    isin: str | None
    name: str
    market: str  # KOSPI / KOSDAQ
    is_preferred: bool
    is_spac: bool
    listed_shares: int | None
    market_cap_krw: int | None
    last_close_krw: int | None
    fetched_at: datetime


class UniverseRegistry:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def upsert_many(self, entries: Iterable[UniverseEntry]) -> int:
        rows = [
            (
                e.code, e.isin, e.name, e.market,
                int(e.is_preferred), int(e.is_spac),
                e.listed_shares, e.market_cap_krw, e.last_close_krw,
                e.fetched_at.isoformat(),
            )
            for e in entries
        ]
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO universe
                (code, isin, name, market, is_preferred, is_spac,
                 listed_shares, market_cap_krw, last_close_krw, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    isin = excluded.isin,
                    name = excluded.name,
                    market = excluded.market,
                    is_preferred = excluded.is_preferred,
                    is_spac = excluded.is_spac,
                    listed_shares = excluded.listed_shares,
                    market_cap_krw = excluded.market_cap_krw,
                    last_close_krw = excluded.last_close_krw,
                    fetched_at = excluded.fetched_at
                """,
                rows,
            )
            self._conn.commit()
        return len(rows)

    def all(
        self,
        *,
        markets: Iterable[str] | None = None,
        exclude_preferred: bool = False,
        exclude_spac: bool = True,
        min_market_cap_krw: int | None = None,
    ) -> list[UniverseEntry]:
        sql = "SELECT * FROM universe WHERE 1=1"
        params: list[object] = []
        if markets:
            placeholders = ",".join("?" * len(list(markets)))
            sql += f" AND market IN ({placeholders})"
            params.extend(markets)
        if exclude_preferred:
            sql += " AND is_preferred = 0"
        if exclude_spac:
            sql += " AND is_spac = 0"
        if min_market_cap_krw is not None:
            sql += " AND market_cap_krw >= ?"
            params.append(min_market_cap_krw)
        sql += " ORDER BY market_cap_krw DESC NULLS LAST, code"
        with self._lock:
            cur = self._conn.execute(sql, params)
            cols = [c[0] for c in cur.description]
            rows = cur.fetchall()
        return [_row_to_entry(dict(zip(cols, row, strict=True))) for row in rows]

    def top_by_market_cap(self, n: int, market: str | None = None) -> list[UniverseEntry]:
        markets = [market] if market else None
        return self.all(markets=markets, exclude_preferred=True, exclude_spac=True)[:n]

    def codes(self, **filter_kwargs) -> list[str]:
        return [e.code for e in self.all(**filter_kwargs)]

    def get(self, code: str) -> UniverseEntry | None:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM universe WHERE code = ?", (code,))
            row = cur.fetchone()
            if row is None:
                return None
            cols = [c[0] for c in cur.description]
        return _row_to_entry(dict(zip(cols, row, strict=True)))


def _row_to_entry(d: dict) -> UniverseEntry:
    return UniverseEntry(
        code=d["code"],
        isin=d["isin"],
        name=d["name"],
        market=d["market"],
        is_preferred=bool(d["is_preferred"]),
        is_spac=bool(d["is_spac"]),
        listed_shares=d["listed_shares"],
        market_cap_krw=d["market_cap_krw"],
        last_close_krw=d["last_close_krw"],
        fetched_at=datetime.fromisoformat(d["fetched_at"]),
    )


def fetch_krx_universe() -> list[UniverseEntry]:
    """Fetch full KOSPI + KOSDAQ via FinanceDataReader. ETF/ETN/ELW already
    excluded by source; SPAC tagged via name match."""
    import FinanceDataReader as fdr  # noqa: PLC0415

    now = datetime.now(UTC)
    entries: list[UniverseEntry] = []
    for market in ("KOSPI", "KOSDAQ"):
        df = fdr.StockListing(market)
        for _, row in df.iterrows():
            code = str(row.get("Code") or "").zfill(6)
            if not code or not code.isdigit():
                continue
            name = str(row.get("Name") or "")
            isin = row.get("ISU_CD")
            is_pref = code[-1] in "5679" and not name.endswith("스팩")
            is_spac = "스팩" in name
            listed_shares = _safe_int(row.get("Stocks"))
            market_cap = _safe_int(row.get("Marcap"))
            last_close = _safe_int(row.get("Close"))
            entries.append(
                UniverseEntry(
                    code=code, isin=isin if isinstance(isin, str) else None,
                    name=name, market=market,
                    is_preferred=is_pref, is_spac=is_spac,
                    listed_shares=listed_shares,
                    market_cap_krw=market_cap,
                    last_close_krw=last_close,
                    fetched_at=now,
                )
            )
    return entries


def _safe_int(v) -> int | None:
    try:
        if v is None:
            return None
        if isinstance(v, float) and (v != v):  # NaN
            return None
        return int(v)
    except (ValueError, TypeError):
        return None
