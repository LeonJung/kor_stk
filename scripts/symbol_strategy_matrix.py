"""symbol_strategy_matrix — backtest 결과를 종목 x strategy 매트릭스로 분해.

목적: paper_trade 에서 종목별로 어떤 strategy 가 잘 맞는지 확인.
- 역헤드앤숄더 가 한화에어로 / SK하이닉스 에 강하다면, 그 조합 boost.
- NR7 이 모든 종목에서 loser 면 strategy 전체 disable.

본 스크립트는 backtest_all_strategies (일봉 696일) 를 실행해서 결과를 매트릭스
로 출력 + 종목별 "trust score" (모든 strategy 누적 +수익 → trust 1.0+, -수익 →
< 1.0) 계산.

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.symbol_strategy_matrix
    PYTHONPATH=src .venv/bin/python -m scripts.symbol_strategy_matrix --days 504
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

_KR = {
    "breakout": "신고가매매", "double_bottom": "쌍바닥",
    "box_breakout": "박스권돌파", "inverse_head_shoulders": "역헤드앤숄더",
    "flag_pennant": "깃발페넌트", "cup_handle": "컵앤핸들",
    "triangle": "삼각수렴", "wedge": "웨지",
    "volatility_breakout": "변동성돌파", "nr7_breakout": "NR7",
    "dual_thrust": "듀얼트러스트", "color_streak": "양봉연속",
    "pivot_half_pullback": "피벗절반",
}
_NAMES = {
    "005930": "삼성전자", "000660": "SK하이닉스", "402340": "SK스퀘어",
    "005380": "현대차", "373220": "LG엔솔", "034020": "두산에너빌",
    "329180": "HD현대중", "028260": "삼성물산", "009150": "삼성전기",
    "207940": "삼성바이오", "012450": "한화에어로", "000270": "기아",
    "105560": "KB금융", "032830": "삼성생명", "006400": "삼성SDI",
    "267260": "HD현대일렉", "010120": "LS ELEC", "055550": "신한지주",
    "012330": "현대모비스", "006800": "미래에셋증권",
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=504, help="일봉 lookback (default 2년)")
    p.add_argument("--top", type=int, default=20)
    args = p.parse_args()

    bs = BarStore("data")
    reg = UniverseRegistry("data/universe.sqlite")
    codes = [e.code for e in reg.top_by_market_cap(args.top)]
    reg.close()

    print(f"\n=== Symbol x Strategy matrix (일봉 {args.days}일, top {args.top}) ===\n")

    all_bars = []
    for sym in codes:
        bars = list(bs.read(sym, "1d"))
        all_bars.extend(bars[-args.days:] if len(bars) >= args.days else bars)

    # Setup
    high60 = compute_high60(bs, codes)
    prev_hl = compute_prev_high_low(bs, codes)
    nr7_setup = compute_nr7_setup(bs, codes)
    color_setup = compute_color_streak_setup(bs, codes, min_streak=3)
    dt_ranges = compute_dual_thrust_ranges(bs, codes, lookback=5)
    pivots = {sym: compute_pivot_levels(list(bs.read(sym, "1d"))[-1])
              for sym in codes if list(bs.read(sym, "1d"))}

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

    # Build items
    items = []
    for bar in all_bars:
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
    for b in all_bars:
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
    print(f"Replay done: {result.total_intents} intents\n")

    # Build (strategy, symbol) → [pnls]
    by_ss: dict[tuple[str, str], list[int]] = defaultdict(list)
    by_ss_positions: dict[tuple[str, str], list[int]] = defaultdict(list)
    for intent, fill_price in result.fills:
        src = intent.sources[0] if intent.sources else "?"
        key = (src, intent.symbol)
        if intent.side.value == "buy":
            by_ss_positions[key].append(fill_price)
        elif intent.side.value == "sell" and by_ss_positions[key]:
            entry = by_ss_positions[key].pop(0)
            by_ss[key].append(fill_price - entry)

    # --- Matrix output ---
    strategies_seen = sorted({s for s, _ in by_ss})
    symbols_seen = sorted({sym for _, sym in by_ss})

    print(f"  {'symbol':<8} ", end="")
    for s in strategies_seen:
        print(f"{_KR.get(s, s)[:6]:>7}", end="")
    print("  TOTAL")
    print("  " + "-" * (10 + 7 * len(strategies_seen) + 10))
    sym_totals: dict[str, int] = {}
    for sym in symbols_seen:
        total = 0
        print(f"  {sym:<8} ", end="")
        for s in strategies_seen:
            pnls = by_ss.get((s, sym), [])
            pnl = sum(pnls)
            total += pnl
            cell = f"{pnl // 1000:+d}k" if pnl else "  ."
            print(f"{cell:>7}", end="")
        sym_totals[sym] = total
        print(f"  {total // 1000:+,d}k")
    print()

    # Trust score per symbol
    print("\n=== Symbol trust score (모든 strategy 누적 +/-) ===\n")
    avg_total = statistics.mean(sym_totals.values()) if sym_totals else 0
    for sym in sorted(sym_totals, key=lambda s: -sym_totals[s]):
        total = sym_totals[sym]
        score = 1.0 + 0.4 * (total - avg_total) / (abs(avg_total) + 1)
        score = max(0.5, min(1.5, score))
        nm = _NAMES.get(sym, "?")
        print(f"  {sym} {nm:<10} total={total // 1000:>+8,}k  trust={score:.2f}")

    # Top combos per strategy
    print("\n=== 종목별 최고 strategy (top 3) ===\n")
    sym_best: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for (s, sym), pnls in by_ss.items():
        sym_best[sym].append((s, sum(pnls)))
    for sym in sorted(sym_best):
        best = sorted(sym_best[sym], key=lambda x: -x[1])[:3]
        nm = _NAMES.get(sym, "?")
        if not best:
            continue
        items_str = ", ".join(f"{_KR.get(s, s)} {pnl // 1000:+,}k"
                              for s, pnl in best if pnl != 0)
        if items_str:
            print(f"  {sym} {nm:<10}: {items_str}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
