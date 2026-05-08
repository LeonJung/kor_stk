"""SQLite ledger — durable record of orders and trades.

The backtest layer keeps positions and trades in memory; live trading
needs persistence so a restart can rebuild state from broker
reconciliation. ``Ledger`` is a small, transactional store backed by
sqlite3 (stdlib, no extra deps) with three tables:

- ``orders``     submitted orders (one row per OrderRouter.submit)
- ``fills``      broker-confirmed fills against an order (1..N per order)
- ``positions``  current quantity / average cost per symbol

Schema is migration-style: ``Ledger(path)`` runs ``CREATE TABLE IF NOT
EXISTS`` on construction so opening an existing file is safe. WAL mode
is enabled for better concurrent reads.

Concurrency: sqlite3 connections are not thread-safe across threads, so
``Ledger`` uses a per-instance ``threading.Lock`` and a single connection.
Multiple Ledgers can point at the same file safely (sqlite handles
locking).
"""

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from ks_ws.domain import OrderIntent, Side
from ks_ws.orders import SubmittedOrder

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    limit_price INTEGER,
    submitted_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'submitted',
    sources TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);
CREATE INDEX IF NOT EXISTS idx_orders_submitted_at ON orders(submitted_at);

CREATE TABLE IF NOT EXISTS fills (
    fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price INTEGER NOT NULL,
    filled_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

CREATE INDEX IF NOT EXISTS idx_fills_order_id ON fills(order_id);
CREATE INDEX IF NOT EXISTS idx_fills_symbol ON fills(symbol);

CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    quantity INTEGER NOT NULL,
    average_cost REAL NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class Ledger:
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

    # Orders -----------------------------------------------------------------

    def record_order(self, submitted: SubmittedOrder) -> None:
        i = submitted.intent
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO orders
                (order_id, symbol, side, quantity, order_type, limit_price,
                 submitted_at, status, sources)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submitted.order_id,
                    i.symbol,
                    str(i.side),
                    i.quantity,
                    i.order_type,
                    i.limit_price,
                    submitted.submitted_at.isoformat(),
                    "submitted",
                    ",".join(i.sources) if i.sources else None,
                ),
            )
            self._conn.commit()

    def list_orders(self, *, symbol: str | None = None) -> list[dict]:
        with self._lock:
            cur = self._conn.cursor()
            if symbol is not None:
                cur.execute(
                    "SELECT * FROM orders WHERE symbol = ? ORDER BY submitted_at",
                    (symbol,),
                )
            else:
                cur.execute("SELECT * FROM orders ORDER BY submitted_at")
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    # Fills ------------------------------------------------------------------

    def record_fill(
        self,
        *,
        order_id: str,
        symbol: str,
        side: Side,
        quantity: int,
        price: int,
        filled_at: datetime | None = None,
    ) -> int:
        ts = (filled_at or datetime.now(UTC)).isoformat()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO fills (order_id, symbol, side, quantity, price, filled_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (order_id, symbol, str(side), quantity, price, ts),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def list_fills(self, *, order_id: str | None = None, symbol: str | None = None) -> list[dict]:
        sql = "SELECT * FROM fills"
        conds: list[str] = []
        params: list[object] = []
        if order_id is not None:
            conds.append("order_id = ?")
            params.append(order_id)
        if symbol is not None:
            conds.append("symbol = ?")
            params.append(symbol)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY filled_at"
        with self._lock:
            cur = self._conn.execute(sql, params)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    # Positions --------------------------------------------------------------

    def upsert_position(self, symbol: str, *, quantity: int, average_cost: float) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO positions (symbol, quantity, average_cost, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    quantity = excluded.quantity,
                    average_cost = excluded.average_cost,
                    updated_at = excluded.updated_at
                """,
                (symbol, quantity, average_cost, datetime.now(UTC).isoformat()),
            )
            self._conn.commit()

    def get_position(self, symbol: str) -> dict | None:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM positions WHERE symbol = ?", (symbol,))
            row = cur.fetchone()
            if row is None:
                return None
            cols = [c[0] for c in cur.description]
            return dict(zip(cols, row, strict=True))

    def list_positions(self) -> list[dict]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM positions ORDER BY symbol")
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def apply_fill(
        self,
        *,
        order_id: str,
        symbol: str,
        side: Side,
        quantity: int,
        price: int,
    ) -> None:
        """Record a fill AND update the running position with weighted avg cost.

        Convenience wrapper for the common case where an external fill event
        feeds both the fills table and the positions snapshot in one call.
        """
        self.record_fill(
            order_id=order_id, symbol=symbol, side=side, quantity=quantity, price=price
        )
        existing = self.get_position(symbol)
        cur_qty = existing["quantity"] if existing else 0
        cur_avg = existing["average_cost"] if existing else 0.0
        if side == Side.BUY:
            new_qty = cur_qty + quantity
            new_avg = (cur_avg * cur_qty + price * quantity) / new_qty if new_qty > 0 else 0.0
        else:
            new_qty = max(0, cur_qty - quantity)
            new_avg = cur_avg if new_qty > 0 else 0.0
        self.upsert_position(symbol, quantity=new_qty, average_cost=new_avg)

    # Helper for OrderIntent -------------------------------------------------

    def build_intent_from_order_row(self, row: dict) -> OrderIntent:
        """Reconstruct an OrderIntent from a row of `orders`. Useful for
        replaying state on startup."""
        return OrderIntent(
            symbol=row["symbol"],
            side=Side(row["side"]),
            quantity=row["quantity"],
            order_type=row["order_type"],
            limit_price=row["limit_price"],
            timestamp=datetime.fromisoformat(row["submitted_at"]),
            sources=tuple(row["sources"].split(",")) if row["sources"] else (),
        )
