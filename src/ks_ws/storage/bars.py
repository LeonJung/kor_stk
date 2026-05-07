"""Parquet-backed Bar storage via DuckDB.

Layout: ``ROOT/bars/{timeframe}/{symbol}/{year}.parquet``

Each (timeframe, symbol, year) bucket is one Parquet file. ``write()``
groups incoming Bars into these buckets, merges with the existing file
if present (DuckDB's ``COPY ... TO`` does not append in place), and
rewrites. This is fine for batch / EOD ingestion; high-frequency tick
append should use a separate SQLite per-day buffer (future) and rotate
to Parquet nightly.

``read()`` uses ``read_parquet`` with optional timestamp filtering and
streams results in chunks so multi-year scans don't pull everything
into memory at once.

Timestamps are stored as DuckDB ``TIMESTAMP`` (naive). Convention: all
inputs are UTC — tz-aware values are converted to UTC then stripped on
write; naive values are assumed already-UTC. Reads always reattach UTC.
"""

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from ks_ws.domain import Bar

_FETCH_CHUNK = 10_000


def _to_storage_utc(ts: datetime) -> datetime:
    """Convert a (possibly tz-aware) datetime to a naive UTC value for DuckDB."""
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(UTC).replace(tzinfo=None)


class BarStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _bucket_path(self, timeframe: str, symbol: str, year: int) -> Path:
        return self.root / "bars" / timeframe / symbol / f"{year}.parquet"

    def write(self, bars: Iterable[Bar]) -> int:
        """Group bars by (timeframe, symbol, year), merge with any existing
        Parquet file in that bucket, and rewrite. Returns total rows written.
        """
        materialized = list(bars)
        if not materialized:
            return 0

        groups: dict[tuple[str, str, int], list[Bar]] = {}
        for b in materialized:
            key = (b.timeframe, b.symbol, b.timestamp.year)
            groups.setdefault(key, []).append(b)

        total = 0
        for (tf, sym, year), group in groups.items():
            path = self._bucket_path(tf, sym, year)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._merge_and_write(path, group)
            total += len(group)
        return total

    def _merge_and_write(self, path: Path, new_bars: list[Bar]) -> None:
        con = duckdb.connect(":memory:")
        try:
            con.execute(
                """
                CREATE TABLE buffer (
                    timestamp TIMESTAMP,
                    open BIGINT,
                    high BIGINT,
                    low BIGINT,
                    close BIGINT,
                    volume BIGINT,
                    value BIGINT
                )
                """
            )
            rows = [
                (
                    _to_storage_utc(b.timestamp),
                    b.open,
                    b.high,
                    b.low,
                    b.close,
                    b.volume,
                    b.value,
                )
                for b in new_bars
            ]
            con.executemany("INSERT INTO buffer VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
            if path.exists():
                con.execute(f"INSERT INTO buffer SELECT * FROM read_parquet('{path}')")
            con.execute(
                f"""
                COPY (
                    SELECT timestamp, open, high, low, close, volume, value
                    FROM buffer
                    ORDER BY timestamp
                ) TO '{path}' (FORMAT PARQUET)
                """
            )
        finally:
            con.close()

    def read(
        self,
        symbol: str,
        timeframe: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Iterator[Bar]:
        """Yield Bars for symbol/timeframe in the half-open range [start, end).
        Streams from all matching year-files in chunks.
        """
        bucket_dir = self.root / "bars" / timeframe / symbol
        if not bucket_dir.exists() or not any(bucket_dir.glob("*.parquet")):
            return

        glob_pattern = str(bucket_dir / "*.parquet")
        sql = (
            "SELECT timestamp, open, high, low, close, volume, value "
            f"FROM read_parquet('{glob_pattern}')"
        )
        params: list[datetime] = []
        conditions = []
        if start is not None:
            conditions.append("timestamp >= ?")
            params.append(_to_storage_utc(start))
        if end is not None:
            conditions.append("timestamp < ?")
            params.append(_to_storage_utc(end))
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY timestamp"

        con = duckdb.connect(":memory:")
        try:
            result = con.execute(sql, params)
            while True:
                rows = result.fetchmany(_FETCH_CHUNK)
                if not rows:
                    break
                for row in rows:
                    ts = row[0]
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=UTC)
                    yield Bar(
                        symbol=symbol,
                        timestamp=ts,
                        timeframe=timeframe,
                        open=row[1],
                        high=row[2],
                        low=row[3],
                        close=row[4],
                        volume=row[5],
                        value=row[6],
                    )
        finally:
            con.close()
