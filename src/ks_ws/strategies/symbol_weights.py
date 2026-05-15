"""SymbolWeightMatrix — strategy × symbol 비중 매트릭스 (Tier 5).

사용자 룰 (2026-05-15): 비중 = 종목별 + 전략별. 회차 ≥ 3 + 승률/평균손익
기준으로 walk-forward weight 계산 (forward-looking 회피). Allocator 가 BUY
quantity 결정 시 곱.

검증된 효과 (vol_breakout 2025-08 ~ 2025-12 test):
- unweighted: n=1639 win%=47.7% pnl=+5.63M
- weighted (walk-forward): n=715 win%=48.0% pnl=+11.17M (1.98배)

스키마 (sqlite):
- table strategy_symbol_weight (strategy, symbol, weight, train_n, train_wins,
  train_pnl_pct_sum, computed_at)
- weight = 0.0 차단 / 1.0 default / 2.0 50-60% / 3.0 60%+

룰 (default):
- train_n < 3 → 0 (표본 부족 차단)
- avg_pnl_pct ≤ 0 → 0 (음수 평균 차단)
- 60%+ 승률 → 3
- 50-60% → 2
- 40-50% → 1
- < 40% → 0
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


_DDL = """
CREATE TABLE IF NOT EXISTS strategy_symbol_weight (
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    weight REAL NOT NULL,
    train_n INTEGER NOT NULL,
    train_wins INTEGER NOT NULL,
    train_pnl_pct_sum REAL NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (strategy, symbol)
);
CREATE INDEX IF NOT EXISTS idx_ssw_strategy ON strategy_symbol_weight(strategy);
"""


@dataclass(frozen=True)
class WeightRule:
    min_n: int = 3
    min_avg_pct: float = 0.0
    w_60: float = 3.0
    w_50: float = 2.0
    w_40: float = 1.0
    default: float = 1.0  # strategy 에 없는 종목 (신규) 기본값


def compute_weight(
    n: int, wins: int, pnl_pct_sum: float, rule: WeightRule | None = None,
) -> float:
    rule = rule or WeightRule()
    if n < rule.min_n:
        return 0.0
    avg = pnl_pct_sum / n if n > 0 else 0.0
    if avg <= rule.min_avg_pct:
        return 0.0
    wr = wins / n
    if wr >= 0.60:
        return rule.w_60
    if wr >= 0.50:
        return rule.w_50
    if wr >= 0.40:
        return rule.w_40
    return 0.0


class SymbolWeightMatrix:
    """In-memory + sqlite-backed strategy × symbol → weight 매트릭스."""

    def __init__(
        self, db_path: str = "data/symbol_weights.sqlite",
        *,
        default: float = 1.0,
    ) -> None:
        self.db_path = db_path
        self.default = default
        self._cache: dict[tuple[str, str], float] = {}
        self._strategy_loaded: set[str] = set()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.executescript(_DDL)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def load(self, strategy: str | None = None) -> int:
        """Load weights from sqlite. None = all strategies. Returns row count."""
        conn = self._conn()
        try:
            if strategy is None:
                rows = conn.execute(
                    "SELECT strategy, symbol, weight FROM strategy_symbol_weight"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT strategy, symbol, weight FROM strategy_symbol_weight "
                    "WHERE strategy = ?", (strategy,),
                ).fetchall()
        finally:
            conn.close()
        for s, sym, w in rows:
            self._cache[(s, sym)] = float(w)
            self._strategy_loaded.add(s)
        return len(rows)

    def weight_for(self, strategy: str, symbol: str) -> float:
        cached = self._cache.get((strategy, symbol))
        if cached is not None:
            return cached
        # If strategy weights loaded but symbol absent → default (new symbol)
        # If strategy not loaded → default (no weight data yet)
        return self.default

    def upsert(
        self, strategy: str, symbol: str, weight: float,
        *, train_n: int = 0, train_wins: int = 0, train_pnl_pct_sum: float = 0.0,
    ) -> None:
        conn = self._conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO strategy_symbol_weight "
                "(strategy, symbol, weight, train_n, train_wins, "
                "train_pnl_pct_sum, computed_at) VALUES (?,?,?,?,?,?,?)",
                (strategy, symbol, weight, train_n, train_wins,
                 train_pnl_pct_sum, datetime.now(UTC).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
        self._cache[(strategy, symbol)] = weight
        self._strategy_loaded.add(strategy)

    def bulk_upsert(self, entries: list[tuple]) -> None:
        """entries: (strategy, symbol, weight, train_n, train_wins, pnl_pct_sum)."""
        conn = self._conn()
        try:
            now = datetime.now(UTC).isoformat()
            conn.executemany(
                "INSERT OR REPLACE INTO strategy_symbol_weight "
                "(strategy, symbol, weight, train_n, train_wins, "
                "train_pnl_pct_sum, computed_at) VALUES (?,?,?,?,?,?,?)",
                [(s, sym, w, n, win, p, now) for s, sym, w, n, win, p in entries],
            )
            conn.commit()
        finally:
            conn.close()
        for s, sym, w, _n, _win, _p in entries:
            self._cache[(s, sym)] = w
            self._strategy_loaded.add(s)

    def stats(self) -> dict[str, dict[str, int]]:
        """Per-strategy weight distribution."""
        out: dict = {}
        for (s, _sym), w in self._cache.items():
            if s not in out:
                out[s] = {"total": 0, "blocked": 0, "x1": 0, "x2": 0, "x3+": 0}
            out[s]["total"] += 1
            if w == 0:
                out[s]["blocked"] += 1
            elif w <= 1.0:
                out[s]["x1"] += 1
            elif w <= 2.0:
                out[s]["x2"] += 1
            else:
                out[s]["x3+"] += 1
        return out
