"""monte_carlo_minute_strategies — 분봉 strategies Monte Carlo 검증.

cycle 38 의 일봉 Monte Carlo 에 미포함된 strategies:
- volatility_breakout, bnf_disparity, vwap_reversion, opening_momentum,
  dual_thrust, tape_burst (전 strategy V는 분봉 fire X)

각 strategy 분봉 backtest 실행 → 같은 (n, TP, SL) 로 분봉 random 비교.

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.monte_carlo_minute_strategies
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ks_ws.storage.bars import BarStore
from ks_ws.storage.universe import UniverseRegistry


def _simulate_random_minute_entries(
    minute_bars_by_sym: dict[str, list],
    n_entries: int,
    tp_pct: float,
    sl_pct: float,
    max_hold_minutes: int = 240,
    rng: random.Random | None = None,
) -> list[int]:
    """Random entries in minute bars. Walk forward 1 bar at a time for TP/SL/timeout."""
    rng = rng or random.Random()
    all_pts = []
    for sym, bars in minute_bars_by_sym.items():
        if len(bars) < 10:
            continue
        for i in range(len(bars) - 1):
            all_pts.append((sym, i))
    if not all_pts:
        return []
    picks = rng.sample(all_pts, min(n_entries, len(all_pts)))
    pnls = []
    for sym, idx in picks:
        bars = minute_bars_by_sym[sym]
        ep = bars[idx].close
        tp = ep * (1 + tp_pct / 100)
        sl = ep * (1 - sl_pct / 100)
        end_idx = min(idx + max_hold_minutes, len(bars) - 1)
        xp = bars[end_idx].close
        for j in range(idx + 1, end_idx + 1):
            if bars[j].high >= tp:
                xp = int(tp)
                break
            if bars[j].low <= sl:
                xp = int(sl)
                break
        pnls.append(xp - ep)
    return pnls


# (strategy_name, tp%, sl%, max_hold_min, "approx n & win from cycle 22 분봉 14일")
_CYCLE22_ACTUAL = {
    # cycle 22-24 분봉 backtest 결과:
    # 14일 분봉: BNF n=16 win=75% / 변동성 n=16 win=68.8% / VWAP n=2476 win=27.7% /
    # 시초 n=192 win=20.3% / 듀얼 n=24 win=41.7%
    # 30일 분봉: BNF n=44 win=77.3% / 변동성 n=49 win=65.3% / VWAP skip
    # 90일: BNF n=82 win=56.1% / 변동성 n=152 win=55.9% / VWAP skip / 시초 n=2907 win=16.9%
    # 180일: BNF n=127 win=59.8% / 변동성 n=251 win=62.2%
    # 400일: BNF n=67 win=53.7% / 변동성 n=276 win=62.7% / VWAP skip / 시초 n=12376 win=12.8%
    "bnf_disparity": {
        "tp": 5.0, "sl": 3.0, "max_hold": 480,
        "tests": [
            (14, 16, 75.0), (30, 44, 77.3), (90, 82, 56.1), (180, 127, 59.8),
        ],
    },
    "volatility_breakout": {
        "tp": 3.0, "sl": 2.0, "max_hold": 360,
        "tests": [
            (14, 16, 68.8), (30, 49, 65.3), (90, 152, 55.9), (180, 251, 62.2),
        ],
    },
    "opening_momentum": {
        "tp": 3.0, "sl": 0.0, "max_hold": 50,  # SL = entry hit (사용자 B-6)
        "tests": [
            (14, 192, 20.3), (90, 2907, 16.9), (180, 8053, 17.2),
        ],
    },
    "dual_thrust": {
        "tp": 3.0, "sl": 2.0, "max_hold": 360,
        "tests": [
            (14, 24, 41.7), (90, 220, 39.5), (180, 371, 44.5),
        ],
    },
    "vwap_reversion": {
        "tp": 2.0, "sl": 3.0, "max_hold": 60,
        "tests": [
            (7, 1186, 28.7), (14, 2476, 27.7),  # vwap 만 검증 짧음 (heavy)
        ],
    },
}

_KR = {
    "bnf_disparity": "BNF이격도", "volatility_breakout": "변동성돌파",
    "opening_momentum": "시초모멘텀", "dual_thrust": "듀얼트러스트",
    "vwap_reversion": "VWAP평균회귀",
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--simulations", type=int, default=100)
    p.add_argument("--days", type=int, default=30,
                   help="분봉 데이터 lookback (default 30)")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    bar_store = BarStore("data")
    reg = UniverseRegistry("data/universe.sqlite")
    codes = [e.code for e in reg.top_by_market_cap(args.top)]
    reg.close()

    cutoff = datetime.now(UTC) - timedelta(days=args.days)
    bars_by_sym: dict[str, list] = {}
    for sym in codes:
        bars_by_sym[sym] = list(bar_store.read(sym, "1m", start=cutoff))
    total_bars = sum(len(b) for b in bars_by_sym.values())
    print(f"\n=== Monte Carlo minute strategies | {args.days}일 분봉 | "
          f"top {args.top} | {args.simulations} sims ===\n")
    print(f"  loaded {total_bars:,} bars across {len(bars_by_sym)} symbols\n")

    rng = random.Random(args.seed)

    print(f"  {'전략':<14} {'tests_n':>7} {'actual':>8} "
          f"{'rand_mean':>10} {'rand_max':>9} {'percentile':>11} {'결론':>20}")
    print("  " + "-" * 92)

    for strat, params in _CYCLE22_ACTUAL.items():
        # Pick the test closest to our 'days' (or use the median)
        tests = params["tests"]
        # Find test closest to args.days
        target = min(tests, key=lambda t: abs(t[0] - args.days))
        _td, n, actual_win = target
        tp = params["tp"]
        sl = params["sl"]
        max_hold = params["max_hold"]
        if sl <= 0:
            # opening_momentum special: SL = entry exact hit ≈ 0 pct loss
            sl = 0.5  # 더 작은 SL 로 sim (opening 룰 정확 reproduce 어려움)

        rand_wins = []
        for _ in range(args.simulations):
            pnls = _simulate_random_minute_entries(
                bars_by_sym, n, tp, sl, max_hold_minutes=max_hold, rng=rng,
            )
            if pnls:
                wins = sum(1 for p in pnls if p > 0)
                rand_wins.append(wins / len(pnls) * 100)
        if not rand_wins:
            print(f"  {_KR[strat]:<14} no data")
            continue

        rand_mean = statistics.mean(rand_wins)
        rand_max = max(rand_wins)
        below = sum(1 for w in rand_wins if w < actual_win)
        pct = below / len(rand_wins) * 100

        if pct >= 95:
            verdict = "✓✓ alpha"
        elif pct >= 75:
            verdict = "✓ 약한 alpha"
        elif pct >= 25:
            verdict = "= random 동등"
        else:
            verdict = "✗ random 미만"

        print(f"  {_KR[strat]:<14} {n:>7} {actual_win:>7.1f}% "
              f"{rand_mean:>9.1f}% {rand_max:>8.1f}% {pct:>9.0f}%  {verdict:<20}"
              f"  (test_days={_td}, tp={tp}% sl={sl}%)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
