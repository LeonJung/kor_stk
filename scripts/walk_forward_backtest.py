"""walk_forward_backtest — 시기별 일관성 검증.

memory `feedback_strategy_validation_priority`:
- 단 1회 696일 backtest = 우연 결과일 수 있음
- 진짜 신뢰도 = 다른 시기에도 일관되게 잘 됨

방법:
- 696일을 N 등분 (default 6 = 4개월 chunk x 6)
- 각 chunk 별로 strategy 마다 backtest 실행
- strategy 별 win_rate / total_pnl 시계열 → mean / std / min / max
- 일관성 평가: std/mean < 0.3 = 일관, > 0.5 = 들쭉날쭉

출력:
- strategy x chunk 매트릭스 (win% / pnl)
- 일관성 점수 (variation coefficient)
- 권고: 신뢰 가능한 strategy / 우연 가능한 strategy 분리

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.walk_forward_backtest
    PYTHONPATH=src .venv/bin/python -m scripts.walk_forward_backtest --chunks 12
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

_STRATEGY_KR = {
    "breakout": "신고가매매", "double_bottom": "쌍바닥", "box_breakout": "박스권돌파",
    "inverse_head_shoulders": "역헤드앤숄더", "flag_pennant": "깃발페넌트",
    "cup_handle": "컵앤핸들", "triangle": "삼각수렴", "wedge": "웨지",
    "volatility_breakout": "변동성돌파", "nr7_breakout": "NR7",
    "dual_thrust": "듀얼트러스트", "color_streak": "양봉연속",
    "pivot_half_pullback": "피벗절반",
}


def _kr(s: str) -> str:
    return _STRATEGY_KR.get(s, s)


def _run_chunk(bars: list, all_codes: list[str], bar_store: BarStore) -> dict[str, dict]:
    """Run 1 backtest chunk → strategy 별 결과 dict."""
    if not bars:
        return {}
    codes = sorted({b.symbol for b in bars})
    high60 = compute_high60(bar_store, codes)
    prev_hl = compute_prev_high_low(bar_store, codes)
    nr7_setup = compute_nr7_setup(bar_store, codes)
    color_setup = compute_color_streak_setup(bar_store, codes, min_streak=3)
    dt_ranges = compute_dual_thrust_ranges(bar_store, codes, lookback=5)
    pivots = {}
    for sym in codes:
        sym_bars = list(bar_store.read(sym, "1d"))
        if sym_bars:
            pivots[sym] = compute_pivot_levels(sym_bars[-1])

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

    # Pre-feed detectors
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

    # Aggregate
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

    out = {}
    for strat, pnls in by_strat.items():
        if not pnls:
            continue
        wins = sum(1 for p in pnls if p > 0)
        out[strat] = {
            "n": len(pnls),
            "win_rate": wins / len(pnls) * 100,
            "total": sum(pnls),
            "mean": int(statistics.mean(pnls)),
        }
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--chunks", type=int, default=6,
                   help="시기 분할 개수 (default 6 = 약 4개월씩)")
    p.add_argument("--days", type=int, default=696,
                   help="총 검증 기간 (default 696일 = 2.7년)")
    p.add_argument("--top", type=int, default=20)
    args = p.parse_args()

    bar_store = BarStore("data")
    reg = UniverseRegistry("data/universe.sqlite")
    codes = [e.code for e in reg.top_by_market_cap(args.top)]
    reg.close()

    print(f"\n=== Walk-forward backtest | {args.chunks} chunks x "
          f"{args.days // args.chunks}일 = {args.days}일 | top {args.top} ===\n")

    # Load all bars, sort by timestamp, slice into chunks
    all_bars = []
    for sym in codes:
        bars = list(bar_store.read(sym, "1d"))
        if len(bars) >= args.days:
            all_bars.extend(bars[-args.days:])
        else:
            all_bars.extend(bars)
    all_bars.sort(key=lambda b: b.timestamp)
    print(f"Total bars: {len(all_bars):,} ({len(codes)} symbols)")

    # Slice by timestamp into chunks
    ts_min = all_bars[0].timestamp
    ts_max = all_bars[-1].timestamp
    total_secs = (ts_max - ts_min).total_seconds()
    chunk_secs = total_secs / args.chunks
    chunks_bars: list[list] = [[] for _ in range(args.chunks)]
    for b in all_bars:
        idx = min(args.chunks - 1,
                  int((b.timestamp - ts_min).total_seconds() / chunk_secs))
        chunks_bars[idx].append(b)

    # Run each chunk
    chunk_results: list[dict[str, dict]] = []
    for i, chunk_bars in enumerate(chunks_bars):
        if not chunk_bars:
            chunk_results.append({})
            continue
        c_min = chunk_bars[0].timestamp.date()
        c_max = chunk_bars[-1].timestamp.date()
        print(f"  Chunk {i + 1}/{args.chunks}: {c_min} ~ {c_max} "
              f"({len(chunk_bars):,} bars)")
        results = _run_chunk(chunk_bars, codes, bar_store)
        chunk_results.append(results)

    # --- Aggregate matrix ---
    all_strategies = sorted({s for r in chunk_results for s in r})
    print("\n=== Strategy x Chunk win_rate matrix ===\n")
    header = "  전략              "
    for i in range(args.chunks):
        header += f" {f'C{i+1}':>6}"
    header += "    mean   std  COV"
    print(header)
    print("  " + "-" * len(header))
    for strat in all_strategies:
        wins = [chunk_results[i].get(strat, {}).get("win_rate", None)
                for i in range(args.chunks)]
        wins_clean = [w for w in wins if w is not None]
        if len(wins_clean) < 2:
            continue
        mean = statistics.mean(wins_clean)
        std = statistics.stdev(wins_clean) if len(wins_clean) > 1 else 0
        cov = std / mean if mean > 0 else 0
        cells = " ".join(f"{w:>5.0f}%" if w is not None else "    ."
                         for w in wins)
        print(f"  {_kr(strat):<18} {cells}   {mean:>5.1f}% "
              f"{std:>5.1f}  {cov:>4.2f}")

    # --- Trust classification ---
    print("\n=== 신뢰도 분류 (COV = std/mean) ===\n")
    print("  COV < 0.3 = 일관 (신뢰), 0.3-0.5 = 보통, > 0.5 = 들쭉날쭉 (우연 가능)\n")
    reliable, ok, unreliable = [], [], []
    for strat in all_strategies:
        wins = [chunk_results[i].get(strat, {}).get("win_rate")
                for i in range(args.chunks)]
        wins_clean = [w for w in wins if w is not None]
        if len(wins_clean) < 3:
            continue  # 3 chunks 이상 fire 한 strategy 만
        mean = statistics.mean(wins_clean)
        std = statistics.stdev(wins_clean)
        cov = std / mean if mean > 0 else 1
        if cov < 0.3 and mean > 45:
            reliable.append((strat, mean, cov))
        elif cov < 0.5:
            ok.append((strat, mean, cov))
        else:
            unreliable.append((strat, mean, cov))

    print("  [신뢰 가능 — COV < 0.3 AND mean > 45%]")
    for s, m, c in sorted(reliable, key=lambda x: x[2]):
        print(f"    ✓ {_kr(s):<18} mean_win={m:.1f}%  COV={c:.2f}")
    print("\n  [보통 — 검토 필요]")
    for s, m, c in sorted(ok, key=lambda x: x[2]):
        print(f"    ~ {_kr(s):<18} mean_win={m:.1f}%  COV={c:.2f}")
    print("\n  [들쭉날쭉 — 우연일 가능성 ↑]")
    for s, m, c in sorted(unreliable, key=lambda x: -x[2]):
        print(f"    ! {_kr(s):<18} mean_win={m:.1f}%  COV={c:.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
