"""WatchlistStore — daily pre-market watchlist persistence.

매일 09:00 직전에 PreMarketWatchlistBuilder 가 생성하는 종목 후보 list 를
SQLite 에 저장. 1 row = 1 (date, version) 의 watchlist. JSON column 으로
symbols + scores + reason 저장 (가벼운 schema, 추가 메타 자유).

Strategy 들 (OpeningMomentum, PairFollow) 이 매일 시작 시 read 해서 watchlist
를 자기 universe 로 사용한다.
"""

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlists (
    date TEXT NOT NULL,
    version INTEGER NOT NULL,
    generated_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (date, version)
);

CREATE INDEX IF NOT EXISTS idx_watchlists_date ON watchlists(date);
"""


@dataclass(frozen=True)
class WatchlistEntry:
    symbol: str
    score: float = 0.0
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Watchlist:
    date: date
    version: int
    generated_at: datetime
    reason: str
    entries: tuple[WatchlistEntry, ...]

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(e.symbol for e in self.entries)


class WatchlistStore:
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

    def save(self, watchlist: Watchlist) -> None:
        payload = json.dumps(
            [
                {"symbol": e.symbol, "score": e.score, "meta": e.meta}
                for e in watchlist.entries
            ],
            ensure_ascii=False,
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO watchlists
                (date, version, generated_at, reason, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    watchlist.date.isoformat(),
                    watchlist.version,
                    watchlist.generated_at.isoformat(),
                    watchlist.reason,
                    payload,
                ),
            )
            self._conn.commit()

    def load_latest(self, target_date: date) -> Watchlist | None:
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT date, version, generated_at, reason, payload
                FROM watchlists
                WHERE date = ?
                ORDER BY version DESC
                LIMIT 1
                """,
                (target_date.isoformat(),),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_watchlist(row)

    def list_dates(self) -> list[date]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT DISTINCT date FROM watchlists ORDER BY date DESC"
            )
            return [date.fromisoformat(r[0]) for r in cur.fetchall()]

    def next_version_for(self, target_date: date) -> int:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM watchlists WHERE date = ?",
                (target_date.isoformat(),),
            )
            return int(cur.fetchone()[0]) + 1


def _row_to_watchlist(row: tuple) -> Watchlist:
    payload = json.loads(row[4])
    entries = tuple(
        WatchlistEntry(symbol=p["symbol"], score=p.get("score", 0.0), meta=p.get("meta", {}))
        for p in payload
    )
    return Watchlist(
        date=date.fromisoformat(row[0]),
        version=int(row[1]),
        generated_at=datetime.fromisoformat(row[2]),
        reason=row[3],
        entries=entries,
    )


def now_utc() -> datetime:
    return datetime.now(UTC)
