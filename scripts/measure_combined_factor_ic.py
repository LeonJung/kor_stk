"""measure_combined_factor_ic — multi-factor 합성 점수의 IC.

Phase A 추가 검증 (사용자 룰 2026-05-15):
- single factor IC 모두 약함 (|IC| < 0.025)
- 합성 점수 (3-factor / 5-factor) 가 더 좋은지 검증
- 단기 mean-reversion 조합 vs 중장기 momentum 조합 분리

조합:
- A. "단기 mean-rev": -value_rvol -value_spike + atr_pct_band (1-5일 horizon)
- B. "중장기 momentum": foreign_5d + near_60d_high + atr_pct_band + consec_green
- C. "모두 단순 평균"
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ks_ws.research.factor_ic import TEST_CUTOFF, compute_factor_ic
from ks_ws.storage.bars import BarStore
from ks_ws.storage.universe import UniverseRegistry

from scripts.measure_factor_ic import (
    _load_bars_by_symbol, _load_foreign_flow_by_symbol, compute_factors,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("measure_combined_factor_ic")


def _zscore_per_day(factor_day_sym: dict) -> dict:
    """각 day 내 cross-sectional z-score 정규화."""
    import statistics
    out: dict = defaultdict(dict)
    for d, sym_v in factor_day_sym.items():
        vals = list(sym_v.values())
        if len(vals) < 5:
            continue
        m = statistics.mean(vals)
        s = statistics.stdev(vals) if len(vals) > 1 else 1.0
        if s <= 0:
            continue
        for sym, v in sym_v.items():
            out[d][sym] = (v - m) / s
    return dict(out)


def _combine(factor_days: list[dict], weights: list[float]) -> dict:
    """day-by-day weighted sum."""
    assert len(factor_days) == len(weights)
    out: dict = defaultdict(dict)
    days = set()
    for fd in factor_days:
        days.update(fd.keys())
    for d in days:
        sym_v: dict[str, float] = {}
        for fd, w in zip(factor_days, weights, strict=False):
            day_dict = fd.get(d, {})
            for sym, v in day_dict.items():
                sym_v[sym] = sym_v.get(sym, 0.0) + w * v
        if sym_v:
            out[d] = sym_v
    return dict(out)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--top", type=int, default=200)
    p.add_argument("--days", type=int, default=700)
    p.add_argument("--forward-days", type=int, default=5)
    args = p.parse_args()

    bar_store = BarStore("data")
    reg = UniverseRegistry("data/universe.sqlite")
    universe = reg.top_by_market_cap(args.top)
    codes = [e.code for e in universe]
    reg.close()
    log.info("universe: %d codes", len(codes))

    bars_by_sym = _load_bars_by_symbol(bar_store, codes, args.days)
    foreign_by_sym = _load_foreign_flow_by_symbol()
    log.info("computing 7 factors ...")
    factors_day, returns_day, _ = compute_factors(
        bars_by_sym, foreign_by_sym, forward_days=args.forward_days,
    )

    # z-score per day
    log.info("z-score normalizing ...")
    zfacs = {name: _zscore_per_day(fd) for name, fd in factors_day.items()}

    combos = {
        # A. 단기 mean-reversion (negative momentum + ATR band)
        "단기_meanrev (-rvol -spike +atr)": _combine(
            [zfacs["value_rvol"], zfacs["value_spike_1d"], zfacs["atr_pct_band"]],
            [-1.0, -1.0, +1.0],
        ),
        # B. 중장기 momentum
        "중장기_momentum (foreign + high + atr + green)": _combine(
            [zfacs["foreign_5d"], zfacs["near_60d_high"],
             zfacs["atr_pct_band"], zfacs["consec_green"]],
            [+1.0, +1.0, +1.0, +0.5],
        ),
        # C. 모두 평균 (사용자 초기 제안)
        "all_avg (foreign+rvol+ma+high+green+atr+spike)": _combine(
            [zfacs["foreign_5d"], zfacs["value_rvol"], zfacs["ma5_over_ma20"],
             zfacs["near_60d_high"], zfacs["consec_green"],
             zfacs["atr_pct_band"], zfacs["value_spike_1d"]],
            [1.0] * 7,
        ),
        # D. ATR band + neg short-term + foreign (mix)
        "mix_filter+rev+foreign": _combine(
            [zfacs["atr_pct_band"], zfacs["value_rvol"], zfacs["foreign_5d"]],
            [+1.0, -1.0, +0.5],
        ),
    }

    print()
    print(f"{'sig':>3} {'factor combo':50} {'mean_IC':>9} {'ICIR':>8} "
          f"{'hit%':>5} {'n_days':>7}")
    print("-" * 95)
    for name, combo in combos.items():
        res = compute_factor_ic(
            name, factor_by_day_sym=combo,
            return_by_day_sym=returns_day,
            min_symbols_per_day=30, train_only=True,
        )
        sig = "✓" if res.significant else " "
        print(f"  {sig} {res.factor_name:50} {res.mean_ic:>+9.4f} "
              f"{res.icir:>+8.2f} {res.hit_rate*100:>4.0f}% {res.n_days:>7}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
