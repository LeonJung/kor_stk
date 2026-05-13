"""TradeReviewLog — strategy 의 entry → exit 페어 회고 누적 저장 (사용자 명시 2026-05-13).

사용자 룰 (`feedback_performance_report_format`):
- strategy 별 종목 그룹화
- 종목별 4 요소: (a) 매수 trigger / (b) 청산 룰 / (c) "과거 돌아간다면" 회고 /
  (d) 시뮬레이션
- (a)(b) 는 코드로 자동 기록. (c)(d) 는 후속 분석에 사용 가능한 데이터.

SQLite 저장. trade 마다 1 row. 매 SELL (TP/SL/timeout/manual) 발생 시 strategy
가 record() 호출.

스키마:
  strategy TEXT, symbol TEXT, entry_ts TEXT, entry_price INTEGER, qty INTEGER,
  exit_ts TEXT, exit_price INTEGER, pnl_krw INTEGER, exit_reason TEXT,
  entry_note TEXT, exit_note TEXT, macro_score_at_entry REAL

API:
- TradeReview dataclass — record contents
- TradeReviewLog(path) — SQLite-backed log with record() + list_reviews()
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    entry_ts TEXT NOT NULL,
    entry_price INTEGER NOT NULL,
    qty INTEGER NOT NULL,
    exit_ts TEXT NOT NULL,
    exit_price INTEGER NOT NULL,
    pnl_krw INTEGER NOT NULL,
    exit_reason TEXT NOT NULL,
    entry_note TEXT,
    exit_note TEXT,
    macro_score_at_entry REAL
);
CREATE INDEX IF NOT EXISTS idx_review_strategy_symbol ON trade_reviews (strategy, symbol);
CREATE INDEX IF NOT EXISTS idx_review_exit_ts ON trade_reviews (exit_ts);
"""


@dataclass
class TradeReview:
    strategy: str
    symbol: str
    entry_ts: datetime
    entry_price: int
    qty: int
    exit_ts: datetime
    exit_price: int
    pnl_krw: int
    exit_reason: str  # "TP" / "SL" / "timeout" / "manual"
    entry_note: str | None = None
    exit_note: str | None = None
    macro_score_at_entry: float | None = None


class TradeReviewLog:
    """SQLite-backed persistent log of strategy entry→exit pairs."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def record(self, review: TradeReview) -> None:
        if review.qty <= 0:
            raise ValueError("qty must be positive")
        d = asdict(review)
        d["entry_ts"] = review.entry_ts.isoformat()
        d["exit_ts"] = review.exit_ts.isoformat()
        self._conn.execute(
            "INSERT INTO trade_reviews (strategy, symbol, entry_ts, entry_price, qty, "
            "exit_ts, exit_price, pnl_krw, exit_reason, entry_note, exit_note, "
            "macro_score_at_entry) VALUES (:strategy, :symbol, :entry_ts, :entry_price, "
            ":qty, :exit_ts, :exit_price, :pnl_krw, :exit_reason, :entry_note, "
            ":exit_note, :macro_score_at_entry)",
            d,
        )
        self._conn.commit()

    def list_reviews(
        self,
        *,
        strategy: str | None = None,
        symbol: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM trade_reviews"
        clauses = []
        params: list[object] = []
        if strategy is not None:
            clauses.append("strategy = ?")
            params.append(strategy)
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY exit_ts DESC"
        if limit is not None:
            if limit <= 0:
                raise ValueError("limit must be positive")
            sql += f" LIMIT {int(limit)}"
        cols = [
            "id", "strategy", "symbol", "entry_ts", "entry_price", "qty",
            "exit_ts", "exit_price", "pnl_krw", "exit_reason",
            "entry_note", "exit_note", "macro_score_at_entry",
        ]
        return [dict(zip(cols, row, strict=True)) for row in self._conn.execute(sql, params)]

    def per_strategy_summary(self) -> dict[str, dict]:
        """Aggregate PnL/win-count/loss-count per strategy."""
        out: dict[str, dict] = {}
        rows = self._conn.execute(
            "SELECT strategy, COUNT(*), SUM(pnl_krw), "
            "SUM(CASE WHEN pnl_krw > 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN pnl_krw < 0 THEN 1 ELSE 0 END) "
            "FROM trade_reviews GROUP BY strategy",
        ).fetchall()
        for strategy, n, total_pnl, wins, losses in rows:
            out[strategy] = {
                "count": n,
                "total_pnl_krw": total_pnl or 0,
                "wins": wins or 0,
                "losses": losses or 0,
                "win_rate": ((wins or 0) / n) if n else 0.0,
            }
        return out

    def close(self) -> None:
        self._conn.close()

    def __len__(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM trade_reviews").fetchone()[0]
