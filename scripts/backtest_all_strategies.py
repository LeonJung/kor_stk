"""backtest_all_strategies — 19 strategy 일괄 일봉 backtest.

데이터:
- BarStore("1d") 최근 N일 (default 252 = 1년)
- 일봉 close 를 가짜 Tick 으로 변환 → tick-기반 strategy 도 fire
- detector 별도 feed → pattern strategies fire

Tier 1 + 2 strategies 가능:
- 7 pattern: double_bottom / box_breakout / inverse_hns / flag_pennant /
  cup_handle / triangle / wedge (detector → event)
- 6 일봉+tick: breakout / volatility_breakout / nr7_breakout / dual_thrust /
  color_streak / pivot_half_pullback (일봉 setup + close cross)
- skip (tick density 필요): vwap_reversion, opening_momentum, tape_burst,
  bnf_disparity (1m), closing_bet (DojiCandle), foreign_flow (event)

출력:
- strategy 별 entry / wins / losses / win% / total_pnl / mean_pnl
- per-symbol breakdown
- worst 3 trades per strategy

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.backtest_all_strategies
    PYTHONPATH=src .venv/bin/python -m scripts.backtest_all_strategies --days 252
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ks_ws.backtest.tick_replay import TickReplayDriver
from ks_ws.detectors.box_breakout import BoxBreakoutDetector
from ks_ws.detectors.cup_handle import CupHandleDetector
from ks_ws.detectors.double_bottom import DoubleBottomDetector
from ks_ws.detectors.flag_pennant import FlagPennantDetector
from ks_ws.detectors.head_shoulders import HeadShouldersDetector
from ks_ws.detectors.triangle import TriangleDetector
from ks_ws.detectors.wedge import WedgeDetector
from ks_ws.domain import Tick
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
    "breakout": "신고가매매",
    "double_bottom": "쌍바닥매매",
    "box_breakout": "박스권돌파매매",
    "inverse_head_shoulders": "역헤드앤숄더매매",
    "flag_pennant": "깃발페넌트매매",
    "cup_handle": "컵앤핸들매매",
    "triangle": "삼각수렴매매",
    "wedge": "웨지매매",
    "volatility_breakout": "변동성돌파",
    "nr7_breakout": "NR7돌파",
    "dual_thrust": "듀얼트러스트",
    "color_streak": "양봉연속",
    "pivot_half_pullback": "피벗절반눌림",
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


def _name(sym: str) -> str:
    return _NAMES.get(sym, "?")


def _kr(s: str) -> str:
    return _STRATEGY_KR.get(s, s)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=252, help="lookback days (default 252 = 1년)")
    p.add_argument("--top", type=int, default=20, help="universe size")
    args = p.parse_args()

    bar_store = BarStore("data")
    reg = UniverseRegistry("data/universe.sqlite")
    universe = reg.top_by_market_cap(args.top)
    codes = [e.code for e in universe]
    reg.close()

    print(f"\n=== backtest_all_strategies | universe top {args.top} | "
          f"lookback {args.days}일 ===\n")

    # --- Load bars for all codes ---
    all_bars = []
    for sym in codes:
        bars = list(bar_store.read(sym, "1d"))
        if len(bars) >= args.days:
            all_bars.extend(bars[-args.days:])
        elif bars:
            all_bars.extend(bars)
    print(f"Loaded {len(all_bars):,} daily bars across {len(codes)} symbols")

    # --- Strategy setups (from history snapshot just before backtest window) ---
    high60 = compute_high60(bar_store, codes)
    prev_hl = compute_prev_high_low(bar_store, codes)
    nr7_setup = compute_nr7_setup(bar_store, codes)
    color_setup = compute_color_streak_setup(bar_store, codes, min_streak=3)
    dt_ranges = compute_dual_thrust_ranges(bar_store, codes, lookback=5)
    pivots = {}
    for sym in codes:
        bars = list(bar_store.read(sym, "1d"))
        if bars:
            pivots[sym] = compute_pivot_levels(bars[-1])

    # --- 13 strategies (skip tick-density ones for daily backtest) ---
    strategies = [
        LiveBreakoutStrategy(high60=high60, take_profit_pct=2.0,
                             stop_loss_pct=3.0, max_hold_minutes=60),
        DoubleBottomStrategy(),
        BoxBreakoutStrategy(),
        InverseHeadShouldersStrategy(),
        FlagPennantStrategy(),
        CupHandleStrategy(),
        TriangleStrategy(),
        WedgeStrategy(),
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
    print(f"Running {len(strategies)} strategies on daily bars...")

    # --- Build items list: each bar close → synthetic Tick + Bar (so both
    #     bar-based detectors AND tick-based strategies fire) ---
    items: list = []
    for bar in all_bars:
        items.append(bar)
        items.append(Tick(
            symbol=bar.symbol, timestamp=bar.timestamp, price=bar.close,
            volume=bar.volume,
        ))

    # --- Detectors emit events; their feed must be called separately.
    # Since TickReplayDriver doesn't auto-feed detectors, manually emit events
    # by feeding detectors before driver runs. The events get published into
    # the driver's bus via `publish` callback.
    # Easier alternative: feed detectors directly into driver.items list as
    # the Bar arrives. But TickReplayDriver doesn't expose that hook.
    # Workaround: run detectors first into a temp bus, capture all events
    # they emit, then include them in the items list.

    from ks_ws.bus import EventBus as _Bus
    from ks_ws.detectors.wedge import WedgeDetected as _WedgeDetected
    from ks_ws.events import (
        BoxBreakoutDetected,
        CupHandleDetected,
        DoubleBottomDetected,
        FlagPennantDetected,
        HeadShouldersDetected,
        TriangleDetected,
    )

    captured_events: list = []
    tmp_bus = _Bus()
    subs = [
        tmp_bus.subscribe(t) for t in (
            DoubleBottomDetected, BoxBreakoutDetected, HeadShouldersDetected,
            FlagPennantDetected, CupHandleDetected, TriangleDetected,
            _WedgeDetected,
        )
    ]
    detectors = [
        DoubleBottomDetector(tmp_bus),
        BoxBreakoutDetector(tmp_bus),
        HeadShouldersDetector(tmp_bus),
        FlagPennantDetector(tmp_bus),
        CupHandleDetector(tmp_bus),
        TriangleDetector(tmp_bus),
        WedgeDetector(tmp_bus),
    ]
    # Feed each symbol's bars in chronological order to each detector
    by_sym = defaultdict(list)
    for b in all_bars:
        by_sym[b.symbol].append(b)
    for _sym, sym_bars in by_sym.items():
        sym_bars.sort(key=lambda b: b.timestamp)
        for bar in sym_bars:
            for det in detectors:
                det.feed(bar)
    # Drain captured events
    for sub in subs:
        while sub.qsize() > 0:
            captured_events.append(sub.get_nowait())
        sub.close()
    print(f"Detectors emitted {len(captured_events)} events")

    items.extend(captured_events)
    items.sort(key=lambda x: x.timestamp)
    print(f"Total items in replay: {len(items):,}")

    # --- Run replay ---
    started = datetime.now(UTC)
    with TickReplayDriver(items, strategies) as driver:
        result = driver.run()
    elapsed = (datetime.now(UTC) - started).total_seconds()
    print(f"Replay done in {elapsed:.1f}s — {result.total_intents} intents, "
          f"{len(result.fills)} fills")

    # --- Aggregate per-strategy from intents (fills) and per-symbol ---
    by_strat: dict[str, list[tuple]] = defaultdict(list)  # strategy → [(symbol, side, price, ts)]
    for intent, fill_price in result.fills:
        src = intent.sources[0] if intent.sources else "?"
        by_strat[src].append((intent.symbol, intent.side.value, fill_price, intent.timestamp))

    # Match BUY+SELL pairs per (strategy, symbol)
    print(f"\n=== Strategy 결과 ({len(by_strat)} strategies fired) ===")
    print(f"  {'전략':<18} {'n':>4} {'wins':>4} {'losses':>4} {'win%':>6} "
          f"{'mean_pnl':>10} {'total_pnl':>14}")
    print("  " + "-" * 76)
    rows = []
    for strat in sorted(by_strat):
        trades = by_strat[strat]
        # pair each BUY with next SELL for same symbol
        positions: dict[str, list[int]] = defaultdict(list)
        pnls: list[int] = []
        for sym, side, price, _ts in trades:
            if side == "buy":
                positions[sym].append(price)
            elif side == "sell" and positions[sym]:
                entry = positions[sym].pop(0)
                pnls.append(price - entry)
        if not pnls:
            continue
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        win_rate = wins / len(pnls) * 100
        mean_pnl = int(statistics.mean(pnls))
        total = sum(pnls)
        rows.append((strat, len(pnls), wins, losses, win_rate, mean_pnl, total))
        print(f"  {_kr(strat):<18} {len(pnls):>4} {wins:>4} {losses:>4} "
              f"{win_rate:>5.1f}% {mean_pnl:>+10,} {total:>+14,}")

    # --- per-symbol top winners/losers per strategy ---
    print("\n=== Per-strategy x 종목 (top 3 winner / top 3 loser) ===")
    for strat in sorted(by_strat):
        trades = by_strat[strat]
        per_sym: dict[str, list[int]] = defaultdict(list)
        # Re-pair per symbol
        positions: dict[str, list[int]] = defaultdict(list)
        for sym, side, price, _ts in trades:
            if side == "buy":
                positions[sym].append(price)
            elif side == "sell" and positions[sym]:
                entry = positions[sym].pop(0)
                per_sym[sym].append(price - entry)
        if not per_sym:
            continue
        symtotals = sorted(
            ((sym, sum(p), len(p)) for sym, p in per_sym.items()),
            key=lambda x: -x[1],
        )
        print(f"\n  📊 {_kr(strat)} ({strat}):")
        for sym, total, n in symtotals[:3]:
            print(f"    + {sym} {_name(sym):<10} pnl={total:>+12,} n={n}")
        if len(symtotals) > 3:
            print("    ...")
            for sym, total, n in symtotals[-3:]:
                print(f"    - {sym} {_name(sym):<10} pnl={total:>+12,} n={n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
