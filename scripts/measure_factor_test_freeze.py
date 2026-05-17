"""measure_factor_test_freeze — Phase D: test 데이터 final 평가.

사용자 룰 (2026-05-17): train+validation (2023-08 ~ 2025-08) 으로 결정한
factor combination 을 test 데이터 (2025-08 ~ 2026-05) 에 적용. 진짜 예측력
검증.

비교:
- Train+Val (cutoff 미적용 + 2025-08 이전만)
- Test (2025-08+ 만)
- 둘 다 비슷한 IC → 진짜 예측 가능
- Test 가 train 보다 크게 낮음 → overfitting

Test combo:
- 단기_meanrev: -z(value_rvol) -z(value_spike_1d) +z(atr_pct_band)
- 중장기_momentum: +z(foreign_5d) +z(near_60d_high) +z(atr_pct_band) +0.5×z(consec_green)
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ks_ws.research.factor_ic import (
    TEST_CUTOFF,
    compute_factor_ic,
)
from ks_ws.storage.bars import BarStore
from ks_ws.storage.universe import UniverseRegistry

from scripts.measure_factor_ic import (
    _load_bars_by_symbol, _load_foreign_flow_by_symbol, compute_factors,
)
from scripts.measure_combined_factor_ic import _combine, _zscore_per_day

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("measure_factor_test_freeze")


def _evaluate_period(
    factor_day_sym: dict, returns_day: dict,
    period_label: str, *,
    start_date: date | None = None, end_date: date | None = None,
    min_symbols: int = 30,
):
    """Calculate IC over specific date window. Returns ICResult."""
    # Filter dates
    f_filtered = {d: v for d, v in factor_day_sym.items()
                  if (start_date is None or d >= start_date)
                  and (end_date is None or d < end_date)}
    r_filtered = {d: v for d, v in returns_day.items()
                  if (start_date is None or d >= start_date)
                  and (end_date is None or d < end_date)}
    return compute_factor_ic(
        period_label, factor_by_day_sym=f_filtered,
        return_by_day_sym=r_filtered,
        min_symbols_per_day=min_symbols, train_only=False,
    )


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

    bars_by_sym = _load_bars_by_symbol(bar_store, codes, args.days)
    foreign_by_sym = _load_foreign_flow_by_symbol()

    # All horizons
    horizons = [1, 5, 10, 20]

    print()
    print(f"=== Phase D: Test freeze 평가 ===")
    print(f"Train+Val: 2023-08 ~ {TEST_CUTOFF}, Test: {TEST_CUTOFF} ~ 2026-05")
    print()

    for fwd in horizons:
        log.info("computing factors for fwd=%d ...", fwd)
        factors_day, returns_day, _ = compute_factors(
            bars_by_sym, foreign_by_sym, forward_days=fwd,
        )
        zfacs = {name: _zscore_per_day(fd) for name, fd in factors_day.items()}

        combos = {
            "단기_meanrev": _combine(
                [zfacs["value_rvol"], zfacs["value_spike_1d"], zfacs["atr_pct_band"]],
                [-1.0, -1.0, +1.0],
            ),
            "중장기_momentum": _combine(
                [zfacs["foreign_5d"], zfacs["near_60d_high"],
                 zfacs["atr_pct_band"], zfacs["consec_green"]],
                [+1.0, +1.0, +1.0, +0.5],
            ),
        }

        print(f"\n--- forward {fwd} days ---")
        print(f"{'combo':>20} {'period':>8} {'mean_IC':>9} {'ICIR':>8} "
              f"{'hit%':>5} {'n_days':>7}")
        for name, combo in combos.items():
            train_res = _evaluate_period(
                combo, returns_day, f"{name}_train",
                end_date=TEST_CUTOFF,
            )
            test_res = _evaluate_period(
                combo, returns_day, f"{name}_test",
                start_date=TEST_CUTOFF,
            )
            # diff
            ic_drop_pct = (
                (test_res.mean_ic - train_res.mean_ic) / abs(train_res.mean_ic) * 100
                if train_res.mean_ic != 0 else 0
            )
            for r, label in [(train_res, "train"), (test_res, "test")]:
                sig = "✓" if r.significant else " "
                print(f"  {sig} {name:>17} {label:>8} {r.mean_ic:>+9.4f} "
                      f"{r.icir:>+8.2f} {r.hit_rate*100:>4.0f}% {r.n_days:>7}")
            # Overfit check
            if train_res.mean_ic > 0 and test_res.mean_ic / train_res.mean_ic < 0.5:
                print(f"    ⚠ overfit 의심 (test/train IC ratio < 0.5)")
            elif train_res.mean_ic > 0 and test_res.mean_ic > 0:
                ratio = test_res.mean_ic / train_res.mean_ic
                print(f"    ✓ test/train IC ratio = {ratio:.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
