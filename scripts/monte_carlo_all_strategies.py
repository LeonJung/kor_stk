"""monte_carlo_all_strategies — 13 일봉 strategies 일괄 Monte Carlo 검증.

흐름:
1. backtest_all_strategies 식으로 13 strategies x 696일 일봉 backtest 1회 실행
2. 각 strategy 별 actual {n, win%, TP, SL} 추출
3. 같은 (n, TP, SL) 로 random entry Monte Carlo N(100) sim
4. actual win 의 random 분포 percentile 산출
5. 분류:
   - percentile >= 95 = significant alpha (실전 사용)
   - 75-95 = 약한 alpha
   - 25-75 = random 동등
   - < 25 = random 미만 (제거)

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.monte_carlo_all_strategies
    PYTHONPATH=src .venv/bin/python -m scripts.monte_carlo_all_strategies \
        --simulations 200 --days 696
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ks_ws.backtest.tick_replay import TickReplayDriver
from ks_ws.bus import EventBus as _Bus
from ks_ws.detectors.box_breakout import BoxBreakoutDetector
from ks_ws.detectors.cup_handle import CupHandleDetector
from ks_ws.detectors.double_bottom import DoubleBottomDetector
from ks_ws.detectors.flag_pennant import FlagPennantDetector
from ks_ws.detectors.head_shoulders import HeadShouldersDetector
from ks_ws.detectors.triangle import TriangleDetector
from ks_ws.detectors.wedge import WedgeDetected as _WedgeDetected
from ks_ws.detectors.wedge import WedgeDetector
from ks_ws.domain import Tick
from ks_ws.events import (
    BoxBreakoutDetected,
    CupHandleDetected,
    DoubleBottomDetected,
    FlagPennantDetected,
    HeadShouldersDetected,
    TriangleDetected,
)
from ks_ws.storage.bars import BarStore
from ks_ws.storage.universe import UniverseRegistry
from ks_ws.strategies.color_streak import (
    ColorStreakStrategy,
    compute_color_streak_setup,
)
from ks_ws.strategies.dual_thrust import (
    DualThrustStrategy,
    compute_dual_thrust_ranges,
)
from ks_ws.strategies.live_breakout import LiveBreakoutStrategy, compute_high60
from ks_ws.strategies.nr7_breakout import (
    NR7BreakoutStrategy,
    compute_nr7_setup,
)
from ks_ws.strategies.pattern_strategies import (
    BoxBreakoutStrategy,
    CupHandleStrategy,
    DoubleBottomStrategy,
    FlagPennantStrategy,
    InverseHeadShouldersStrategy,
    TriangleStrategy,
    WedgeStrategy,
)
from ks_ws.strategies.pivot_half_pullback import (
    PivotHalfPullbackStrategy,
    compute_pivot_levels,
)
from ks_ws.strategies.volatility_breakout import (
    VolatilityBreakoutStrategy,
    compute_prev_high_low,
)

_KR = {
    "breakout": "신고가매매", "double_bottom": "쌍바닥", "box_breakout": "박스권돌파",
    "inverse_head_shoulders": "역헤드앤숄더", "flag_pennant": "깃발페넌트",
    "cup_handle": "컵앤핸들", "triangle": "삼각수렴", "wedge": "웨지",
    "volatility_breakout": "변동성돌파", "nr7_breakout": "NR7",
    "dual_thrust": "듀얼트러스트", "color_streak": "양봉연속",
    "pivot_half_pullback": "피벗절반",
}

# Strategy → (TP%, SL%, max_hold_min) — paper_trade default 와 동일
_PARAMS = {
    "breakout": (2.0, 3.0, 60),
    "double_bottom": (3.0, 2.0, 240),
    "box_breakout": (3.0, 2.0, 240),
    "inverse_head_shoulders": (3.0, 2.0, 240),
    "flag_pennant": (3.0, 2.0, 240),
    "cup_handle": (3.0, 2.0, 240),
    "triangle": (3.0, 2.0, 240),
    "wedge": (3.0, 2.0, 240),
    "volatility_breakout": (3.0, 2.0, 360),
    "nr7_breakout": (3.0, 2.0, 360),
    "dual_thrust": (3.0, 2.0, 360),
    "color_streak": (3.0, 2.0, 360),
    "pivot_half_pullback": (2.5, 2.0, 240),
}


def _simulate_random_entries(
    bars_by_sym: dict[str, list],
    n_entries: int,
    tp_pct: float,
    sl_pct: float,
    max_hold_days: int = 30,
    rng: random.Random | None = None,
) -> list[int]:
    rng = rng or random.Random()
    all_pts = []
    for sym, bars in bars_by_sym.items():
        if len(bars) < 5:
            continue
        for i in range(len(bars) - 1):
            all_pts.append((sym, i))
    if not all_pts:
        return []
    picks = rng.sample(all_pts, min(n_entries, len(all_pts)))
    pnls = []
    for sym, idx in picks:
        bars = bars_by_sym[sym]
        ep = bars[idx].close
        tp = ep * (1 + tp_pct / 100)
        sl = ep * (1 - sl_pct / 100)
        end_idx = min(idx + max_hold_days, len(bars) - 1)
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


def _run_actual_backtest(bars: list, bar_store: BarStore, codes: list[str]):
    """Single backtest → strategy 별 (n, win_rate, total)."""
    high60 = compute_high60(bar_store, codes)
    prev_hl = compute_prev_high_low(bar_store, codes)
    nr7_setup = compute_nr7_setup(bar_store, codes)
    color_setup = compute_color_streak_setup(bar_store, codes, min_streak=3)
    dt_ranges = compute_dual_thrust_ranges(bar_store, codes, lookback=5)
    pivots = {}
    for sym in codes:
        sb = list(bar_store.read(sym, "1d"))
        if sb:
            pivots[sym] = compute_pivot_levels(sb[-1])

    strategies = [
        LiveBreakoutStrategy(high60=high60, take_profit_pct=2.0,
                             stop_loss_pct=3.0, max_hold_minutes=60),
        DoubleBottomStrategy(), BoxBreakoutStrategy(),
        InverseHeadShouldersStrategy(), FlagPennantStrategy(),
        CupHandleStrategy(), TriangleStrategy(), WedgeStrategy(),
        VolatilityBreakoutStrategy(prev_high_low=prev_hl, k=0.5,
                                   take_profit_pct=3.0, stop_loss_pct=2.0),
        NR7BreakoutStrategy(setup=nr7_setup, take_profit_pct=3.0,
                            stop_loss_pct=2.0),
        DualThrustStrategy(ranges=dt_ranges, k1=0.5, k2=0.5,
                           take_profit_pct=3.0, stop_loss_pct=2.0),
        ColorStreakStrategy(setup=color_setup, take_profit_pct=3.0,
                            stop_loss_pct=2.0),
        PivotHalfPullbackStrategy(pivots=pivots, take_profit_pct=2.5,
                                  stop_loss_pct=2.0),
    ]

    items = []
    for bar in bars:
        items.append(bar)
        items.append(Tick(symbol=bar.symbol, timestamp=bar.timestamp,
                          price=bar.close, volume=bar.volume))

    tmp_bus = _Bus(default_maxsize=500_000)
    subs = [tmp_bus.subscribe(t, maxsize=500_000) for t in (
        DoubleBottomDetected, BoxBreakoutDetected, HeadShouldersDetected,
        FlagPennantDetected, CupHandleDetected, TriangleDetected,
        _WedgeDetected,
    )]
    dets = [DoubleBottomDetector(tmp_bus), BoxBreakoutDetector(tmp_bus),
            HeadShouldersDetector(tmp_bus), FlagPennantDetector(tmp_bus),
            CupHandleDetector(tmp_bus), TriangleDetector(tmp_bus),
            WedgeDetector(tmp_bus)]
    by_sym = defaultdict(list)
    for b in bars:
        by_sym[b.symbol].append(b)
    for _sym, sb in by_sym.items():
        sb.sort(key=lambda b: b.timestamp)
        for bar in sb:
            for det in dets:
                det.feed(bar)
    events = []
    for sub in subs:
        while sub.qsize() > 0:
            events.append(sub.get_nowait())
        sub.close()
    items.extend(events)
    items.sort(key=lambda x: x.timestamp)

    with TickReplayDriver(items, strategies) as driver:
        result = driver.run()

    by_strat = defaultdict(list)
    positions = defaultdict(list)
    for intent, price in result.fills:
        src = intent.sources[0] if intent.sources else "?"
        key = (src, intent.symbol)
        if intent.side.value == "buy":
            positions[key].append(price)
        elif intent.side.value == "sell" and positions[key]:
            entry = positions[key].pop(0)
            by_strat[src].append(price - entry)

    out = {}
    for strat, pnls in by_strat.items():
        if not pnls:
            continue
        wins = sum(1 for p in pnls if p > 0)
        out[strat] = {
            "n": len(pnls),
            "win": wins / len(pnls) * 100,
            "total": sum(pnls),
        }
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--simulations", type=int, default=100)
    p.add_argument("--days", type=int, default=696)
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    bar_store = BarStore("data")
    reg = UniverseRegistry("data/universe.sqlite")
    codes = [e.code for e in reg.top_by_market_cap(args.top)]
    reg.close()

    all_bars = []
    bars_by_sym = {}
    for sym in codes:
        bars = list(bar_store.read(sym, "1d"))
        window = bars[-args.days:] if len(bars) >= args.days else bars
        bars_by_sym[sym] = window
        all_bars.extend(window)
    all_bars.sort(key=lambda b: b.timestamp)

    print(f"\n=== Monte Carlo all strategies | {args.days}일 | "
          f"top {args.top} | {args.simulations} sims/strategy ===\n")

    print("[1/2] Actual backtest 실행...")
    actual = _run_actual_backtest(all_bars, bar_store, codes)
    print(f"  {len(actual)} strategies fired\n")

    print("[2/2] Random Monte Carlo 비교...")
    rng = random.Random(args.seed)

    print(f"\n  {'전략':<14} {'n':>4} {'actual':>8} "
          f"{'rand_mean':>10} {'rand_max':>9} {'percentile':>11} {'결론':>20}")
    print("  " + "-" * 90)

    classify = {"alpha": [], "weak": [], "random": [], "underperform": []}
    for strat, params in _PARAMS.items():
        if strat not in actual:
            continue
        n = actual[strat]["n"]
        actual_win = actual[strat]["win"]
        tp, sl, _ = params

        # Run N random sims with same n, tp, sl
        rand_wins = []
        for _ in range(args.simulations):
            pnls = _simulate_random_entries(
                bars_by_sym, n, tp_pct=tp, sl_pct=sl, rng=rng,
            )
            if pnls:
                wins = sum(1 for p in pnls if p > 0)
                rand_wins.append(wins / len(pnls) * 100)
        if not rand_wins:
            continue

        rand_mean = statistics.mean(rand_wins)
        rand_max = max(rand_wins)
        below = sum(1 for w in rand_wins if w < actual_win)
        pct = below / len(rand_wins) * 100

        if pct >= 95:
            verdict = "✓✓ alpha"
            classify["alpha"].append(strat)
        elif pct >= 75:
            verdict = "✓ 약한 alpha"
            classify["weak"].append(strat)
        elif pct >= 25:
            verdict = "= random 동등"
            classify["random"].append(strat)
        else:
            verdict = "✗ random 미만"
            classify["underperform"].append(strat)

        print(f"  {_KR.get(strat, strat):<14} {n:>4} {actual_win:>7.1f}% "
              f"{rand_mean:>9.1f}% {rand_max:>8.1f}% {pct:>9.0f}%  {verdict:<20}")

    print("\n=== 분류 ===\n")
    print(f"  ✓✓ alpha ({len(classify['alpha'])}): "
          f"{', '.join(_KR.get(s, s) for s in classify['alpha'])}")
    print(f"  ✓ 약한 alpha ({len(classify['weak'])}): "
          f"{', '.join(_KR.get(s, s) for s in classify['weak'])}")
    print(f"  = random 동등 ({len(classify['random'])}): "
          f"{', '.join(_KR.get(s, s) for s in classify['random'])}")
    print(f"  ✗ random 미만 ({len(classify['underperform'])}): "
          f"{', '.join(_KR.get(s, s) for s in classify['underperform'])}")

    print("\n  실전 매매 권고: alpha (>= 95 percentile) strategies 만 활성")
    return 0


if __name__ == "__main__":
    sys.exit(main())
