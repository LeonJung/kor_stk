"""monte_carlo_baseline — strategy vs random entry baseline 비교.

memory `feedback_strategy_validation_priority`:
- strategy 가 진짜 alpha 인지 vs 우연인지 검증
- Method: 같은 시기에 random entry timing 으로 N번 simulation → win_rate
  분포. strategy 의 실제 win_rate 가 이 random 분포의 어디 위치?
- 95% percentile 이상 = statistically significant alpha
- median 근처 = random 과 다를 바 없음 = 진짜 alpha 없음

방법:
- 696일 일봉 backtest 의 strategy 가 emit 한 entry timestamp 수집 (예: 역헤드앤
  숄더 146 events)
- 그 수의 random entry timestamp 를 N (예: 1000) 번 sampling, 각각 같은 TP/SL
  로 backtest → win_rate 분포
- 실제 strategy win_rate 가 random 분포의 어느 percentile 인가

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.monte_carlo_baseline
    PYTHONPATH=src .venv/bin/python -m scripts.monte_carlo_baseline \
        --strategy inverse_head_shoulders --simulations 100
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ks_ws.storage.bars import BarStore
from ks_ws.storage.universe import UniverseRegistry


def _simulate_random_entries(
    bars_by_sym: dict[str, list],
    n_entries: int,
    take_profit_pct: float,
    stop_loss_pct: float,
    max_hold_days: int = 30,
    rng: random.Random | None = None,
) -> list[int]:
    """N개의 random entry 시점에서 TP/SL 까지 hold → PnL 리스트."""
    rng = rng or random.Random()
    all_entry_points = []
    for sym, bars in bars_by_sym.items():
        if len(bars) < 5:
            continue
        # entry 가능한 bar index = 0 ~ len-1 (max_hold 까지 hold 가능)
        for i in range(len(bars) - 1):
            all_entry_points.append((sym, i))
    if not all_entry_points:
        return []

    picks = rng.sample(all_entry_points, min(n_entries, len(all_entry_points)))
    pnls = []
    for sym, idx in picks:
        bars = bars_by_sym[sym]
        entry_price = bars[idx].close
        tp_price = entry_price * (1 + take_profit_pct / 100)
        sl_price = entry_price * (1 - stop_loss_pct / 100)
        # walk forward until TP/SL hit or max_hold
        end_idx = min(idx + max_hold_days, len(bars) - 1)
        exit_price = bars[end_idx].close  # default: max_hold close
        for j in range(idx + 1, end_idx + 1):
            if bars[j].high >= tp_price:
                exit_price = int(tp_price)
                break
            if bars[j].low <= sl_price:
                exit_price = int(sl_price)
                break
        pnls.append(exit_price - entry_price)
    return pnls


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--simulations", type=int, default=100,
                   help="Monte Carlo simulation 수 (default 100)")
    p.add_argument("--days", type=int, default=696)
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--n-entries", type=int, default=146,
                   help="각 simulation 의 entry 수 (default 146 = 역H&S backtest)")
    p.add_argument("--actual-win", type=float, default=51.2,
                   help="비교할 actual strategy win rate %% (default 51.2 = 역H&S TP2.5/SL2.5)")
    p.add_argument("--actual-strategy-name", default="역헤드앤숄더 TP2.5/SL2.5")
    p.add_argument("--tp", type=float, default=2.5)
    p.add_argument("--sl", type=float, default=2.5)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    bar_store = BarStore("data")
    reg = UniverseRegistry("data/universe.sqlite")
    codes = [e.code for e in reg.top_by_market_cap(args.top)]
    reg.close()

    bars_by_sym: dict[str, list] = {}
    for sym in codes:
        bars = list(bar_store.read(sym, "1d"))
        bars_by_sym[sym] = bars[-args.days:] if len(bars) >= args.days else bars

    print(f"\n=== Monte Carlo baseline | {args.simulations} sims | "
          f"{args.n_entries} entries each | TP={args.tp}% SL={args.sl}% ===\n")

    rng = random.Random(args.seed)
    win_rates = []
    total_pnls = []
    for i in range(args.simulations):
        pnls = _simulate_random_entries(
            bars_by_sym, args.n_entries,
            take_profit_pct=args.tp, stop_loss_pct=args.sl, rng=rng,
        )
        if not pnls:
            continue
        wins = sum(1 for p in pnls if p > 0)
        wr = wins / len(pnls) * 100
        win_rates.append(wr)
        total_pnls.append(sum(pnls))
        if (i + 1) % 20 == 0:
            print(f"  sim {i + 1}/{args.simulations}: "
                  f"random win={wr:.1f}% pnl={sum(pnls):+,}")

    print(f"\n=== Random baseline 분포 (n={len(win_rates)} sims) ===\n")
    win_rates.sort()
    total_pnls.sort()
    if win_rates:
        print(f"  Win%   mean={statistics.mean(win_rates):.1f} "
              f"std={statistics.stdev(win_rates):.1f} "
              f"min={min(win_rates):.1f} max={max(win_rates):.1f}")
        pcts = [5, 25, 50, 75, 95]
        for pct in pcts:
            idx = int(len(win_rates) * pct / 100)
            print(f"    {pct}% percentile: win={win_rates[idx]:.1f}%")
        # Where does actual win sit?
        actual = args.actual_win
        below = sum(1 for w in win_rates if w < actual)
        percentile = below / len(win_rates) * 100
        print(f"\n  Actual ({args.actual_strategy_name}) win={actual:.1f}% "
              f"= random {percentile:.0f} percentile")
        if percentile >= 95:
            print("    → significant alpha (random 보다 분명 우수)")
        elif percentile >= 75:
            print("    → 약한 alpha (random 보다 약간 우수)")
        elif percentile >= 25:
            print("    → 의미 없음 (random 과 동등)")
        else:
            print("    → underperforms (random 보다 못함)")

    if total_pnls:
        print(f"\n  PnL    mean={int(statistics.mean(total_pnls)):+,} "
              f"std={int(statistics.stdev(total_pnls)):,} "
              f"min={min(total_pnls):+,} max={max(total_pnls):+,}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
