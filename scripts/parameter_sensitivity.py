"""parameter_sensitivity — TP/SL grid backtest.

memory `feedback_strategy_validation_priority`:
- 좁은 parameter 범위에서만 잘 됨 = overfitting
- 넓은 범위 robust = 진짜 신뢰

대상: walk-forward 신뢰 strategies (삼각수렴 + 역헤드앤숄더). 다른 strategies
도 grid 가능 — argparse 로 --strategy 선택.

Grid:
- take_profit_pct in [1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
- stop_loss_pct in [1.0, 1.5, 2.0, 2.5, 3.0]
- 696일 일봉 backtest 각 combo

Output:
- TP x SL heat map (win_rate / total_pnl)
- robust 영역 (모든 cell 이 +수익 + win > 50%) 식별
- best cell + 주변 cells 의 일관성

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.parameter_sensitivity
    PYTHONPATH=src .venv/bin/python -m scripts.parameter_sensitivity \
        --strategy inverse_head_shoulders --days 504
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ks_ws.backtest.tick_replay import TickReplayDriver
from ks_ws.bus import EventBus as _Bus
from ks_ws.detectors.head_shoulders import HeadShouldersDetector
from ks_ws.detectors.triangle import TriangleDetector
from ks_ws.domain import Tick
from ks_ws.events import HeadShouldersDetected, TriangleDetected
from ks_ws.storage.bars import BarStore
from ks_ws.storage.universe import UniverseRegistry
from ks_ws.strategies.pattern_strategies import (
    InverseHeadShouldersStrategy,
    TriangleStrategy,
)


def _run_one(strategy_factory, bars: list, events: list) -> dict:
    """Single backtest. Returns dict(n, win, total, mean)."""
    items = []
    for bar in bars:
        items.append(bar)
        items.append(Tick(symbol=bar.symbol, timestamp=bar.timestamp,
                          price=bar.close, volume=bar.volume))
    items.extend(events)
    items.sort(key=lambda x: x.timestamp)

    with TickReplayDriver(items, [strategy_factory()]) as driver:
        result = driver.run()

    by_strat: dict[str, list[int]] = defaultdict(list)
    positions: dict[tuple[str, str], list[int]] = defaultdict(list)
    for intent, price in result.fills:
        src = intent.sources[0] if intent.sources else "?"
        key = (src, intent.symbol)
        if intent.side.value == "buy":
            positions[key].append(price)
        elif intent.side.value == "sell" and positions[key]:
            entry = positions[key].pop(0)
            by_strat[src].append(price - entry)
    if not by_strat:
        return {"n": 0, "win": 0.0, "total": 0, "mean": 0}
    pnls = next(iter(by_strat.values()))
    wins = sum(1 for p in pnls if p > 0)
    return {
        "n": len(pnls),
        "win": wins / len(pnls) * 100 if pnls else 0.0,
        "total": sum(pnls),
        "mean": int(statistics.mean(pnls)) if pnls else 0,
    }


_STRATEGY_REGISTRY = {
    "triangle": {
        "kr": "삼각수렴",
        "detector_cls": TriangleDetector,
        "event_cls": TriangleDetected,
        "strategy_cls": TriangleStrategy,
    },
    "inverse_head_shoulders": {
        "kr": "역헤드앤숄더",
        "detector_cls": HeadShouldersDetector,
        "event_cls": HeadShouldersDetected,
        "strategy_cls": InverseHeadShouldersStrategy,
    },
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--strategy", default="triangle",
                   choices=list(_STRATEGY_REGISTRY))
    p.add_argument("--days", type=int, default=696)
    p.add_argument("--top", type=int, default=20)
    args = p.parse_args()

    bar_store = BarStore("data")
    reg = UniverseRegistry("data/universe.sqlite")
    codes = [e.code for e in reg.top_by_market_cap(args.top)]
    reg.close()

    spec = _STRATEGY_REGISTRY[args.strategy]
    print(f"\n=== Parameter sensitivity | {spec['kr']} | "
          f"{args.days}일 x top {args.top} ===\n")

    all_bars = []
    for sym in codes:
        bars = list(bar_store.read(sym, "1d"))
        all_bars.extend(bars[-args.days:] if len(bars) >= args.days else bars)
    all_bars.sort(key=lambda b: b.timestamp)
    print(f"Loaded {len(all_bars):,} bars")

    # Pre-feed detector ONCE
    tmp_bus = _Bus(default_maxsize=500_000)
    sub = tmp_bus.subscribe(spec["event_cls"], maxsize=500_000)
    detector = spec["detector_cls"](tmp_bus)
    by_sym = defaultdict(list)
    for b in all_bars:
        by_sym[b.symbol].append(b)
    for _sym, sb in by_sym.items():
        sb.sort(key=lambda b: b.timestamp)
        for bar in sb:
            detector.feed(bar)
    events = []
    while sub.qsize() > 0:
        events.append(sub.get_nowait())
    sub.close()
    print(f"Detector emitted {len(events)} events\n")

    tp_grid = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
    sl_grid = [1.0, 1.5, 2.0, 2.5, 3.0]

    # win% matrix
    print("  [Win% matrix] (rows = TP%, cols = SL%)")
    print(f"  {'TP\\SL':<6}", end="")
    for sl in sl_grid:
        print(f" {sl:>5.1f}%", end="")
    print()
    win_grid: dict[tuple[float, float], dict] = {}
    for tp in tp_grid:
        print(f"  {tp:>5.1f}%", end="")
        for sl in sl_grid:
            def factory(tp_val=tp, sl_val=sl):
                # max_hold 충분히 길게 (일봉 backtest 에선 default 240min 이
                # 1봉 = 1440min < timeout 으로 즉시 fire). TP/SL 만 sensitivity.
                return spec["strategy_cls"](
                    take_profit_pct=tp_val, stop_loss_pct=sl_val,
                    max_hold_minutes=60 * 24 * 30,  # 30일
                )
            r = _run_one(factory, all_bars, events)
            win_grid[(tp, sl)] = r
            if r["n"] > 0:
                print(f" {r['win']:>5.1f}%", end="")
            else:
                print(f" {'  -':>6}", end="")
        print()

    # total_pnl matrix
    print("\n  [Total PnL matrix]")
    print(f"  {'TP\\SL':<6}", end="")
    for sl in sl_grid:
        print(f" {sl:>6.1f}%", end="")
    print()
    for tp in tp_grid:
        print(f"  {tp:>5.1f}%", end="")
        for sl in sl_grid:
            r = win_grid[(tp, sl)]
            if r["n"] > 0:
                print(f" {r['total'] // 1000:>+6,}k", end="")
            else:
                print(f" {'    -':>7}", end="")
        print()

    # Robustness analysis
    print("\n=== Robustness 분석 ===\n")
    pnls = [r["total"] for r in win_grid.values() if r["n"] > 0]
    wins = [r["win"] for r in win_grid.values() if r["n"] > 0]
    pos_cells = sum(1 for r in win_grid.values() if r["n"] > 0 and r["total"] > 0)
    win50_cells = sum(1 for r in win_grid.values() if r["n"] > 0 and r["win"] >= 50)
    total_cells = sum(1 for r in win_grid.values() if r["n"] > 0)
    if total_cells == 0:
        print("  no data")
        return 0
    print(f"  Active cells: {total_cells}/{len(tp_grid) * len(sl_grid)}")
    print(f"  +PnL cells: {pos_cells}/{total_cells} ({pos_cells / total_cells * 100:.0f}%)")
    print(f"  win >= 50% cells: {win50_cells}/{total_cells} "
          f"({win50_cells / total_cells * 100:.0f}%)")
    if pnls:
        print(f"  PnL mean={int(statistics.mean(pnls)):+,} "
              f"std={int(statistics.stdev(pnls)) if len(pnls) > 1 else 0:,}")
    if wins:
        print(f"  Win mean={statistics.mean(wins):.1f}% "
              f"std={statistics.stdev(wins) if len(wins) > 1 else 0:.1f}")

    # Best cell + neighbors
    best = max(win_grid.items(), key=lambda x: x[1]["total"])
    bt = best[0]
    print(f"\n  Best cell: TP={bt[0]}% SL={bt[1]}% → win={best[1]['win']:.1f}% "
          f"total={best[1]['total']:+,}")
    # Find neighbors of best (TP ±1 row, SL ±1 col)
    tp_idx = tp_grid.index(bt[0])
    sl_idx = sl_grid.index(bt[1])
    neighbor_pnls = []
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            ni, nj = tp_idx + di, sl_idx + dj
            if 0 <= ni < len(tp_grid) and 0 <= nj < len(sl_grid):
                cell = win_grid.get((tp_grid[ni], sl_grid[nj]))
                if cell and cell["n"] > 0:
                    neighbor_pnls.append(cell["total"])
    if len(neighbor_pnls) >= 5:
        nm = statistics.mean(neighbor_pnls)
        ns = statistics.stdev(neighbor_pnls)
        cov = ns / abs(nm) if nm else 1
        print(f"  Best 주변 9 cells: mean={int(nm):+,} std={int(ns):,} "
              f"COV={cov:.2f}")
        if cov < 0.3:
            print("    → 주변도 일관 = robust (진짜 신뢰)")
        elif cov < 0.7:
            print("    → 주변도 비슷 = 보통")
        else:
            print("    → best 만 peak, 주변 들쭉날쭉 = overfitting 의심")

    return 0


if __name__ == "__main__":
    sys.exit(main())
