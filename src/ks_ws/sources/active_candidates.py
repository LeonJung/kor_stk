"""ActiveCandidateRanker — UniverseExpander 누적 결과 → top K 활성 종목.

cycle 11 task 5 의 후속 (cycle 11 task 6 = cycle 12 task 1).

흐름:
- UniverseExpander 가 매 5분 ``data/universe_candidates.sqlite`` 의 candidates
  table 에 surge_ratio + 시각 row 누적.
- 다음 paper_trade 시작 시 (= 매일 07:50 cron) 이 누적을 분석:
  - 최근 N 일 (default 7) 의 row 들 → 종목별 max surge_ratio + count
  - score = max_surge * log(count+1)  — 강도 + 빈도 가중
  - top K 반환
- paper_trade 의 universe 합성:
  ``codes = top_market_cap(top_market_cap_n) + top_active(top_active_n)``
  (dedup, KIS WS subscription max 20 안에서)

memory ``feedback_multi_symbol`` 룰 — universe 좁힘 X. KIS WS 한도 (20) 안에서는
시총 top 15 + 활성 top 5 조합이 안정 + 발굴 둘 다 잡는 절충.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger("ks_ws.sources.active_candidates")


@dataclass(frozen=True)
class ActiveCandidate:
    symbol: str
    max_surge_ratio: float
    count: int
    score: float


def top_active_candidates(
    db_path: Path | str,
    *,
    days: int = 7,
    top_k: int = 5,
    exclude_codes: set[str] | None = None,
    now_utc: datetime | None = None,
) -> list[ActiveCandidate]:
    """Read ``universe_candidates.sqlite`` and rank symbols by recent surge.

    - ``days`` — lookback window (default 7).
    - ``top_k`` — number of candidates to return.
    - ``exclude_codes`` — already-in-universe codes to skip (top market-cap dedupe).
    - score = max_surge * log(count+1).
    """
    db = Path(db_path)
    if not db.exists():
        log.warning("active candidates DB not found: %s", db)
        return []
    if days <= 0 or top_k <= 0:
        raise ValueError("days and top_k must be positive")
    exclude = exclude_codes or set()
    now = now_utc or datetime.now(UTC)
    since = (now - timedelta(days=days)).isoformat()

    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT symbol, MAX(surge_ratio), COUNT(*) "
            "FROM candidates WHERE detected_at >= ? GROUP BY symbol",
            (since,),
        ).fetchall()
    finally:
        conn.close()

    items = []
    for sym, max_surge, count in rows:
        if sym in exclude:
            continue
        score = float(max_surge) * math.log(count + 1)
        items.append(ActiveCandidate(
            symbol=sym, max_surge_ratio=float(max_surge),
            count=int(count), score=score,
        ))
    items.sort(key=lambda c: -c.score)
    return items[:top_k]
