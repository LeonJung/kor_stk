"""JournalSystem (Sec 15) — 매매 종료 후 reflection 슬롯 + 회고 누적.

book Sec 15: 매매일지가 도박과 기술의 차이. ks_ws Ledger 는 fills/orders 만
저장 → 그 위에 **회고 메타** (entry_reason, exit_reason, setup_tag,
market_regime, lesson) 를 저장하는 별도 SQLite 테이블.

각 trade (round-trip) 종료 시 caller (또는 Claude 회고 세션) 가 entry 를
기록. 자동 분류는 안 함 (사용자 D-9: Claude 가 직접 회고).

Schema:
- trade_id (auto)
- symbol, strategy, opened_at, closed_at, qty, entry_price, exit_price, pnl_krw
- entry_reason, exit_reason, setup_tag, market_regime, lesson (free-text)
"""

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    entry_price INTEGER NOT NULL,
    exit_price INTEGER NOT NULL,
    pnl_krw INTEGER NOT NULL,
    entry_reason TEXT NOT NULL DEFAULT '',
    exit_reason TEXT NOT NULL DEFAULT '',
    setup_tag TEXT NOT NULL DEFAULT '',
    market_regime TEXT NOT NULL DEFAULT '',
    lesson TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_journal_strategy ON journal(strategy);
CREATE INDEX IF NOT EXISTS idx_journal_symbol ON journal(symbol);
CREATE INDEX IF NOT EXISTS idx_journal_closed_at ON journal(closed_at);
"""


@dataclass(frozen=True)
class JournalEntry:
    trade_id: int
    symbol: str
    strategy: str
    opened_at: datetime
    closed_at: datetime
    quantity: int
    entry_price: int
    exit_price: int
    pnl_krw: int
    entry_reason: str = ""
    exit_reason: str = ""
    setup_tag: str = ""
    market_regime: str = ""
    lesson: str = ""


class JournalSystem:
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

    def record(
        self,
        *,
        symbol: str,
        strategy: str,
        opened_at: datetime,
        closed_at: datetime,
        quantity: int,
        entry_price: int,
        exit_price: int,
        pnl_krw: int,
        entry_reason: str = "",
        exit_reason: str = "",
        setup_tag: str = "",
        market_regime: str = "",
        lesson: str = "",
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO journal (
                    symbol, strategy, opened_at, closed_at, quantity,
                    entry_price, exit_price, pnl_krw,
                    entry_reason, exit_reason, setup_tag, market_regime, lesson
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol, strategy,
                    opened_at.isoformat(), closed_at.isoformat(),
                    quantity, entry_price, exit_price, pnl_krw,
                    entry_reason, exit_reason, setup_tag, market_regime, lesson,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def annotate(
        self,
        trade_id: int,
        *,
        entry_reason: str | None = None,
        exit_reason: str | None = None,
        setup_tag: str | None = None,
        market_regime: str | None = None,
        lesson: str | None = None,
    ) -> None:
        """Update a recorded trade's reflection fields (for Claude follow-up
        sessions that read trades and add lessons)."""
        fields: dict[str, str] = {}
        if entry_reason is not None:
            fields["entry_reason"] = entry_reason
        if exit_reason is not None:
            fields["exit_reason"] = exit_reason
        if setup_tag is not None:
            fields["setup_tag"] = setup_tag
        if market_regime is not None:
            fields["market_regime"] = market_regime
        if lesson is not None:
            fields["lesson"] = lesson
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        params = list(fields.values()) + [trade_id]
        with self._lock:
            self._conn.execute(
                f"UPDATE journal SET {sets} WHERE trade_id = ?", params
            )
            self._conn.commit()

    def all(self, *, strategy: str | None = None, symbol: str | None = None) -> list[JournalEntry]:
        sql = "SELECT * FROM journal"
        conds: list[str] = []
        params: list[object] = []
        if strategy is not None:
            conds.append("strategy = ?")
            params.append(strategy)
        if symbol is not None:
            conds.append("symbol = ?")
            params.append(symbol)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY closed_at"
        with self._lock:
            cur = self._conn.execute(sql, params)
            cols = [c[0] for c in cur.description]
            rows = cur.fetchall()
        return [_row_to_entry(dict(zip(cols, row, strict=True))) for row in rows]

    def needs_reflection(self) -> list[JournalEntry]:
        """Return all journal entries with empty entry_reason or lesson —
        candidates for Claude review session."""
        return [e for e in self.all() if not e.entry_reason or not e.lesson]


def _row_to_entry(d: dict) -> JournalEntry:
    return JournalEntry(
        trade_id=int(d["trade_id"]),
        symbol=d["symbol"],
        strategy=d["strategy"],
        opened_at=datetime.fromisoformat(d["opened_at"]),
        closed_at=datetime.fromisoformat(d["closed_at"]),
        quantity=int(d["quantity"]),
        entry_price=int(d["entry_price"]),
        exit_price=int(d["exit_price"]),
        pnl_krw=int(d["pnl_krw"]),
        entry_reason=d["entry_reason"] or "",
        exit_reason=d["exit_reason"] or "",
        setup_tag=d["setup_tag"] or "",
        market_regime=d["market_regime"] or "",
        lesson=d["lesson"] or "",
    )
