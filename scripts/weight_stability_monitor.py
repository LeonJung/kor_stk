"""weight_stability_monitor — SymbolWeightMatrix 주간 변동성 measure.

사용자 룰 (2026-05-17): factor combo overfit 확인 후, Tier 5 weight 도 같은
위험. 매주 재갱신 시 weight 가 너무 자주 바뀌면 = unstable signal = 신뢰도 ↓.

측정:
- 어제 weight vs 오늘 weight: 종목별 변화 분포
- 일관된 ×3 종목 (consistent winners) = robust
- ×0 ↔ ×3 자주 토글 종목 = 표본 부족 또는 unstable

활용:
- 너무 자주 바뀌는 종목 = weight 동결 또는 default 1.0
- consistent ×3 종목만 진짜 booster 로 사용
"""

from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ks_ws.strategies.symbol_weights import SymbolWeightMatrix

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("weight_stability_monitor")


def snapshot_weights(db_path: str) -> dict[tuple[str, str], float]:
    """현재 db 의 (strategy, symbol) → weight 매핑 dump."""
    if not Path(db_path).exists():
        return {}
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT strategy, symbol, weight FROM strategy_symbol_weight"
        ).fetchall()
    finally:
        conn.close()
    return {(s, sym): float(w) for s, sym, w in rows}


def save_snapshot_csv(snap: dict, out_path: str) -> None:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "symbol", "weight", "snapshot_at"])
        ts = datetime.now(UTC).isoformat()
        for (strat, sym), wt in sorted(snap.items()):
            w.writerow([strat, sym, wt, ts])


def compare(prev: dict, cur: dict) -> dict:
    """Per-strategy 변화 통계."""
    by_strat: dict[str, dict] = defaultdict(lambda: {
        "total": 0, "unchanged": 0, "weight_changed": 0,
        "new_added": 0, "removed": 0,
        "to_zero": 0, "from_zero": 0,
    })
    all_keys = set(prev) | set(cur)
    for k in all_keys:
        strat = k[0]
        b = by_strat[strat]
        b["total"] += 1
        if k not in prev:
            b["new_added"] += 1
            if cur[k] > 0:
                b["from_zero"] += 1
        elif k not in cur:
            b["removed"] += 1
            if prev[k] > 0:
                b["to_zero"] += 1
        else:
            p = prev[k]; c = cur[k]
            if p == c:
                b["unchanged"] += 1
            else:
                b["weight_changed"] += 1
                if p > 0 and c == 0:
                    b["to_zero"] += 1
                if p == 0 and c > 0:
                    b["from_zero"] += 1
    return dict(by_strat)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/symbol_weights.sqlite")
    p.add_argument("--snapshot-dir", default="data/weight_snapshots")
    p.add_argument("--compare-with", default="",
                   help="이전 snapshot CSV 경로 (compare mode)")
    args = p.parse_args()

    cur = snapshot_weights(args.db)
    if not cur:
        log.warning("db empty or not found: %s", args.db)
        return 1
    log.info("current snapshot: %d entries", len(cur))

    # Save today's snapshot
    today_iso = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    snap_path = f"{args.snapshot_dir}/snapshot_{today_iso}.csv"
    save_snapshot_csv(cur, snap_path)
    log.info("snapshot saved: %s", snap_path)

    # Compare with previous if given
    if args.compare_with and Path(args.compare_with).exists():
        prev = {}
        with open(args.compare_with) as f:
            for r in csv.DictReader(f):
                prev[(r["strategy"], r["symbol"])] = float(r["weight"])
        log.info("comparing with %s (%d entries)", args.compare_with, len(prev))
        diffs = compare(prev, cur)
        print()
        print(f"{'strategy':22} {'total':>6} {'same':>6} {'chg':>5} "
              f"{'new':>4} {'rm':>4} {'→0':>4} {'0→':>4} {'stability':>10}")
        print("-" * 80)
        for strat in sorted(diffs):
            b = diffs[strat]
            stab = b["unchanged"] / b["total"] * 100 if b["total"] else 0
            print(f"{strat:22} {b['total']:>6} {b['unchanged']:>6} "
                  f"{b['weight_changed']:>5} {b['new_added']:>4} "
                  f"{b['removed']:>4} {b['to_zero']:>4} {b['from_zero']:>4} "
                  f"{stab:>9.1f}%")
        print()
        # Aggregate
        tot = sum(b["total"] for b in diffs.values())
        same = sum(b["unchanged"] for b in diffs.values())
        chg = sum(b["weight_changed"] for b in diffs.values())
        print(f"전체: total={tot} unchanged={same} ({same/tot*100:.1f}%) "
              f"changed={chg} ({chg/tot*100:.1f}%)")
    else:
        # First snapshot — list newest top weights
        log.info("first snapshot — no comparison")
        print()
        print("Per-strategy weight distribution:")
        by_strat_w: dict[str, dict] = defaultdict(lambda: defaultdict(int))
        for (strat, _sym), w in cur.items():
            by_strat_w[strat][w] += 1
        for strat in sorted(by_strat_w):
            dist = by_strat_w[strat]
            print(f"  {strat:22} ", end="")
            for w in sorted(dist.keys(), reverse=True):
                print(f" w={w:.1f}:{dist[w]:>4}", end="")
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
