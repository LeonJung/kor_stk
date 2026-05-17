"""measure_sector_rotation_ic — sector rotation 의 예측력 검증.

Phase B 검증 (사용자 룰 2026-05-17): const blacklist 가 아니라 동적 sector
rotation 으로 가야 함. 진짜로 어제 leading sector → 오늘/내일 leading 이
유지되는지 (momentum) 또는 lagging 으로 빠지는지 (reversal) 정량.

측정:
1. Sector 일별 강도 (구성종목 일별 수익률 평균)
2. Auto-correlation (어제 ranking → 오늘 ranking Spearman)
3. T+N forward sector return IC (sector strength → 다음 N일 sector return)
4. Lead-lag matrix: 어제 leading 3 → 오늘 leading top 종목군 overlap

train_only=True (cutoff 2025-08-01).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import UTC, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ks_ws.research.factor_ic import (
    TEST_CUTOFF,
    spearman_rank_correlation,
)
from ks_ws.sources.sector import SectorClassifier
from ks_ws.storage.bars import BarStore
from ks_ws.storage.universe import UniverseRegistry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("measure_sector_rotation_ic")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--top", type=int, default=200)
    p.add_argument("--days", type=int, default=700)
    args = p.parse_args()

    bar_store = BarStore("data")
    reg = UniverseRegistry("data/universe.sqlite")
    universe = reg.top_by_market_cap(args.top)
    codes = [e.code for e in universe]
    reg.close()
    log.info("universe: %d codes", len(codes))

    classifier = SectorClassifier()  # default 30+ mapping
    classified = sum(1 for c in codes if classifier.classify(c) != "unknown")
    log.info("classifier 매핑: %d/%d (%.1f%%)", classified, len(codes),
             classified / len(codes) * 100)

    # 일봉 close 시계열 종목별 로드
    closes: dict[str, dict[date, int]] = {}
    for c in codes:
        bars = list(bar_store.read(c, "1d"))
        if len(bars) < 30:
            continue
        recent = bars[-args.days:]
        closes[c] = {b.timestamp.astimezone(UTC).date(): b.close for b in recent}

    # 모든 거래일 집합
    all_days = sorted({d for sym in closes for d in closes[sym]})
    log.info("총 거래일: %d (%s ~ %s)", len(all_days), all_days[0], all_days[-1])

    # 매일 sector strength 계산 (1일 return = 어제 close vs 오늘 close)
    # sector_strength[date][sector] = mean(sym return)
    sector_strength: dict[date, dict[str, float]] = {}
    for i in range(1, len(all_days)):
        today = all_days[i]
        yesterday = all_days[i-1]
        sec_returns: dict[str, list[float]] = defaultdict(list)
        for sym, day_close in closes.items():
            sec = classifier.classify(sym)
            if sec == "unknown":
                continue
            c0 = day_close.get(yesterday)
            c1 = day_close.get(today)
            if c0 and c1 and c0 > 0:
                ret = (c1 - c0) / c0
                sec_returns[sec].append(ret)
        if sec_returns:
            sector_strength[today] = {
                sec: sum(rs) / len(rs) for sec, rs in sec_returns.items() if rs
            }

    log.info("sector strength 일자: %d", len(sector_strength))

    # 1) Auto-correlation: ranking[t] vs ranking[t+lag] Spearman
    sorted_dates = sorted(d for d in sector_strength
                          if d < TEST_CUTOFF)
    print()
    print("=== Sector ranking auto-correlation (train period, %d days) ==="
          % len(sorted_dates))
    print(f"{'lag':>4} {'mean_corr':>11} {'hit%(>0)':>9}")
    print("-" * 32)
    for lag in [1, 3, 5, 10, 20]:
        corrs = []
        for i, d in enumerate(sorted_dates):
            if i + lag >= len(sorted_dates):
                break
            d2 = sorted_dates[i + lag]
            sectors_today = sector_strength[d]
            sectors_future = sector_strength[d2]
            common = sorted(set(sectors_today) & set(sectors_future))
            if len(common) < 5:
                continue
            xs = [sectors_today[s] for s in common]
            ys = [sectors_future[s] for s in common]
            c = spearman_rank_correlation(xs, ys)
            if c != 0.0 or sum(xs) != 0:  # filter degenerate
                corrs.append(c)
        if corrs:
            mc = sum(corrs) / len(corrs)
            hit = sum(1 for c in corrs if c > 0) / len(corrs)
            print(f"{lag:>4} {mc:>+11.4f} {hit*100:>8.0f}% (n={len(corrs)})")

    # 2) Sector strength → forward N-day sector return IC
    # forward = (sector mean close at day+N) / (sector mean close at day) - 1
    def sector_avg_close(d):
        out = {}
        for sym, day_close in closes.items():
            sec = classifier.classify(sym)
            if sec == "unknown":
                continue
            c = day_close.get(d)
            if c:
                out.setdefault(sec, []).append(c)
        return {sec: sum(cs) / len(cs) for sec, cs in out.items() if cs}

    print()
    print("=== Sector strength → forward N-day sector return IC ===")
    print(f"{'fwd':>4} {'mean_IC':>11} {'hit%':>7} {'n':>5}")
    print("-" * 32)
    for fwd in [1, 3, 5, 10, 20]:
        ics = []
        for i, d in enumerate(sorted_dates):
            if i + fwd >= len(sorted_dates):
                break
            d_future = sorted_dates[i + fwd]
            strength = sector_strength[d]
            avg_today = sector_avg_close(d)
            avg_future = sector_avg_close(d_future)
            common = sorted(set(strength) & set(avg_today) & set(avg_future))
            if len(common) < 5:
                continue
            xs = [strength[s] for s in common]
            ys = [(avg_future[s] - avg_today[s]) / avg_today[s]
                  for s in common if avg_today[s] > 0]
            if len(xs) != len(ys):
                continue
            ic = spearman_rank_correlation(xs, ys)
            ics.append(ic)
        if ics:
            mi = sum(ics) / len(ics)
            hit = sum(1 for c in ics if c > 0) / len(ics)
            print(f"{fwd:>4} {mi:>+11.4f} {hit*100:>6.0f}% {len(ics):>5}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
