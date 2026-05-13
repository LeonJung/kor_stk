"""UniverseExpander — 장중 거래대금 폭증 종목 자동 발굴.

memory ``feedback_multi_symbol`` + 사용자 의견 (2026-05-13): paper/live 모두 여러
종목 동시 탐색, universe 좁힘 X. top 20 외에도 거래대금 폭증한 종목은 universe
후보로 자동 발굴.

V1 = candidate 검출 + 누적 log. (KIS WS subscription swap 은 V2 — mid-session
swap 위험.)

설계:
- input = ``candidate_codes`` 시총 상위 100-200 종목 코드 list. BarStore("1m")
  에 분봉 데이터가 있어야 함 (Daishin sync 로 누적).
- 매 ``interval_sec`` 마다 ``scan()`` 호출:
  - 각 종목 최근 N 분봉 (recent_window) value 합 = recent_value
  - 그 전 4N 분봉 (baseline_window) value 평균 = baseline_value
  - surge_ratio = recent_value / baseline_value
  - surge_ratio ≥ threshold (default 3.0) 인 종목을 ExpansionCandidate 로 emit
- EventBus 에 새 event ``UniverseCandidateDetected`` publish + SQLite 누적
  (``data/universe_candidates.sqlite``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ks_ws.bus import EventBus
from ks_ws.events import Event
from ks_ws.storage.bars import BarStore

log = logging.getLogger("ks_ws.sources.universe_expander")


class UniverseCandidateDetected(Event):
    """거래대금 폭증 종목 발견 event."""

    surge_ratio: float
    recent_value_krw: int
    baseline_value_krw: int
    recent_minutes: int


@dataclass(frozen=True)
class ExpansionCandidate:
    symbol: str
    surge_ratio: float
    recent_value_krw: int
    baseline_value_krw: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    surge_ratio REAL NOT NULL,
    recent_value_krw INTEGER NOT NULL,
    baseline_value_krw INTEGER NOT NULL,
    recent_minutes INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidates_sym ON candidates(symbol);
CREATE INDEX IF NOT EXISTS idx_candidates_ts ON candidates(detected_at);
"""


class UniverseExpander:
    def __init__(
        self,
        bus: EventBus,
        bar_store: BarStore,
        candidate_codes: Sequence[str],
        *,
        recent_window_min: int = 15,
        baseline_window_min: int = 60,
        surge_threshold: float = 3.0,
        interval_sec: float = 300.0,
        log_db: Path | str | None = "data/universe_candidates.sqlite",
    ) -> None:
        if recent_window_min <= 0 or baseline_window_min <= recent_window_min:
            raise ValueError(
                "baseline_window_min must be > recent_window_min > 0",
            )
        if surge_threshold <= 1.0:
            raise ValueError("surge_threshold must be > 1.0")
        if interval_sec <= 0:
            raise ValueError("interval_sec must be positive")
        self._bus = bus
        self._bar_store = bar_store
        self._codes = list(candidate_codes)
        self.recent_window = recent_window_min
        self.baseline_window = baseline_window_min
        self.surge_threshold = surge_threshold
        self.interval_sec = interval_sec
        self._db_path: Path | None = Path(log_db) if log_db else None
        self._conn: sqlite3.Connection | None = None
        if self._db_path is not None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self._task: asyncio.Task[None] | None = None
        self.scan_count = 0
        self.candidates_seen: set[str] = set()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def scan(self) -> list[ExpansionCandidate]:
        out: list[ExpansionCandidate] = []
        now = datetime.now(UTC)
        for sym in self._codes:
            bars = list(self._bar_store.read(sym, "1m"))
            if len(bars) < self.baseline_window + self.recent_window:
                continue
            recent = bars[-self.recent_window:]
            baseline = bars[
                -(self.recent_window + self.baseline_window):
                -self.recent_window
            ]
            recent_total = sum(b.value for b in recent)
            baseline_per_window = (
                sum(b.value for b in baseline)
                / len(baseline)
                * self.recent_window
            )
            if baseline_per_window <= 0:
                continue
            ratio = recent_total / baseline_per_window
            if ratio < self.surge_threshold:
                continue
            cand = ExpansionCandidate(
                symbol=sym, surge_ratio=ratio,
                recent_value_krw=int(recent_total),
                baseline_value_krw=int(baseline_per_window),
            )
            out.append(cand)
            self._bus.publish(UniverseCandidateDetected(
                symbol=sym, timestamp=now,
                surge_ratio=ratio,
                recent_value_krw=int(recent_total),
                baseline_value_krw=int(baseline_per_window),
                recent_minutes=self.recent_window,
            ))
            if self._conn is not None:
                with contextlib.suppress(Exception):
                    self._conn.execute(
                        "INSERT INTO candidates (detected_at, symbol, "
                        "surge_ratio, recent_value_krw, baseline_value_krw, "
                        "recent_minutes) VALUES (?, ?, ?, ?, ?, ?)",
                        (now.isoformat(), sym, ratio,
                         int(recent_total), int(baseline_per_window),
                         self.recent_window),
                    )
                    self._conn.commit()
            self.candidates_seen.add(sym)
        self.scan_count += 1
        log.info(
            "universe_expander scan %d: %d/%d candidates "
            "(threshold=%.1fx surge)",
            self.scan_count, len(out), len(self._codes),
            self.surge_threshold,
        )
        return out

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.to_thread(self.scan)
                await asyncio.sleep(self.interval_sec)
        except asyncio.CancelledError:
            pass
