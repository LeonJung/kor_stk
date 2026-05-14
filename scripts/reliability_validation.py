"""reliability_validation — 전체 19 strategies 신뢰성 종합 검증.

사용자 명시 (2026-05-14):
- 전체 전략, 비축된 모든 데이터로 backtest
- 승률 너무 높으면 에러/버그 자동 의심
- 시스템 신뢰성 우선 (승률 X)
- 통과한 strategy 만 현금 매매

검증 5단계:
1. 일봉 backtest (696일, 13 strategies)
2. 분봉 backtest (30일, tick-based strategies)
3. Walk-forward (6 chunks, 일관성 COV)
4. Monte Carlo (random baseline percentile)
5. **버그 자동 탐지**:
   - win_rate > 95% AND n >= 5 = 의심 (random 도 100% 가능, 또는 bug)
   - 단일 trade pnl > entry * 30% = pre-feed 같은 비현실 trade 의심
   - 같은 종목 같은 분 안에 N번 entry = same-day single entry 없음
   - actual win 이 random max 보다 위 = 통계적으로 매우 의심 (또는 진짜 alpha)
   - sample n < 10 = statistically insignificant

최종 등급:
- **A 신뢰**: walk-forward COV < 0.3 + Monte Carlo > 95 percentile + 버그 없음 + n >= 30
- **B 통과**: 위 일부 충족
- **C 의심**: 버그 의심 또는 통계 부족
- **D 비활성**: random 미만 또는 명확한 버그

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.reliability_validation
    PYTHONPATH=src .venv/bin/python -m scripts.reliability_validation --output reliability_report.md
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
from collections import defaultdict
from datetime import UTC, datetime
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
from ks_ws.stats.wilson_ci import wilson_ci
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


def _random_baseline(bars_by_sym, n, tp, sl, max_hold_days=30,
                     simulations=100, rng=None) -> list[float]:
    """Run N Monte Carlo sims, return list of win_rates."""
    rng = rng or random.Random(42)
    all_pts = []
    for sym, bars in bars_by_sym.items():
        if len(bars) < 5:
            continue
        for i in range(len(bars) - 1):
            all_pts.append((sym, i))
    if not all_pts:
        return []
    wins_list = []
    for _ in range(simulations):
        picks = rng.sample(all_pts, min(n, len(all_pts)))
        pnls = []
        for sym, idx in picks:
            bars = bars_by_sym[sym]
            ep = bars[idx].close
            tp_p = ep * (1 + tp / 100)
            sl_p = ep * (1 - sl / 100)
            end = min(idx + max_hold_days, len(bars) - 1)
            xp = bars[end].close
            for j in range(idx + 1, end + 1):
                if bars[j].high >= tp_p:
                    xp = int(tp_p)
                    break
                if bars[j].low <= sl_p:
                    xp = int(sl_p)
                    break
            pnls.append(xp - ep)
        if pnls:
            wins_list.append(sum(1 for p in pnls if p > 0) / len(pnls) * 100)
    return wins_list


def _run_daily_backtest(all_bars, codes, bar_store):
    """Run full daily backtest with all 13 strategies + detector pre-feed."""
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
    for bar in all_bars:
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

    # Per strategy → per trade list (entry / exit / pnl / dates)
    by_strat: dict[str, list[dict]] = defaultdict(list)
    positions: dict[tuple[str, str], list[tuple[int, datetime]]] = defaultdict(list)
    for intent, price in result.fills:
        src = intent.sources[0] if intent.sources else "?"
        key = (src, intent.symbol)
        if intent.side.value == "buy":
            positions[key].append((price, intent.timestamp))
        elif intent.side.value == "sell" and positions[key]:
            entry_p, entry_t = positions[key].pop(0)
            by_strat[src].append({
                "symbol": intent.symbol,
                "entry_price": entry_p, "entry_ts": entry_t,
                "exit_price": price, "exit_ts": intent.timestamp,
                "pnl": price - entry_p,
            })
    return by_strat


def _detect_bugs(strat: str, trades: list[dict]) -> list[str]:
    """Auto-detect suspicious patterns. Returns list of warnings."""
    bugs = []
    if not trades:
        return bugs

    # 1. 단일 trade 가 entry * 30% 이상 movement = 비현실
    extreme = [t for t in trades if abs(t["pnl"]) > t["entry_price"] * 0.3]
    if extreme:
        bugs.append(f"extreme_move ({len(extreme)} trades, >30% pnl)")

    # 2. win_rate > 95% AND n >= 5
    wins = sum(1 for t in trades if t["pnl"] > 0)
    win_rate = wins / len(trades) * 100
    if win_rate > 95 and len(trades) >= 5:
        bugs.append(f"too_perfect ({win_rate:.1f}% win, n={len(trades)})")

    # 3. 같은 symbol 같은 분 안에 동시 entry — same-day single entry 없음 의심
    by_sym_min: dict[tuple[str, str], int] = defaultdict(int)
    for t in trades:
        key = (t["symbol"], t["entry_ts"].strftime("%Y%m%d-%H%M"))
        by_sym_min[key] += 1
    over_entries = [k for k, c in by_sym_min.items() if c > 1]
    if over_entries:
        bugs.append(f"multi_entry_same_min ({len(over_entries)} cases)")

    # 4. entry/exit 시간 차이 극단 (< 1초 또는 > 1년) — 시간 처리 버그
    weird_time = []
    for t in trades:
        delta = (t["exit_ts"] - t["entry_ts"]).total_seconds()
        if delta < 1 or delta > 86400 * 365:
            weird_time.append(t)
    if weird_time:
        bugs.append(f"weird_holding_time ({len(weird_time)} trades)")

    return bugs


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=696)
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--simulations", type=int, default=100)
    p.add_argument("--output", default="reliability_report.md")
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
    print(f"Loaded {len(all_bars):,} daily bars across {len(bars_by_sym)} symbols")

    print("\n[1/2] Daily backtest 실행 (13 strategies, pre-feed)...")
    started = datetime.now(UTC)
    by_strat = _run_daily_backtest(all_bars, codes, bar_store)
    elapsed = (datetime.now(UTC) - started).total_seconds()
    print(f"  {len(by_strat)} strategies fired, {elapsed:.1f}s elapsed")

    # Default TP/SL per strategy (paper_trade default)
    _PARAMS = {
        "breakout": (2.0, 3.0),
        "double_bottom": (3.0, 2.0),
        "box_breakout": (3.0, 2.0),
        "inverse_head_shoulders": (3.0, 2.0),
        "flag_pennant": (3.0, 2.0),
        "cup_handle": (3.0, 2.0),
        "triangle": (3.0, 2.0),
        "wedge": (3.0, 2.0),
        "volatility_breakout": (3.0, 2.0),
        "nr7_breakout": (3.0, 2.0),
        "dual_thrust": (3.0, 2.0),
        "color_streak": (3.0, 2.0),
        "pivot_half_pullback": (2.5, 2.0),
    }

    rng = random.Random(42)

    rows = []
    print("\n[2/2] Per strategy 신뢰성 검증...")
    for strat, trades in by_strat.items():
        params = _PARAMS.get(strat, (3.0, 2.0))
        tp, sl = params

        # Actual metrics
        n = len(trades)
        wins = sum(1 for t in trades if t["pnl"] > 0)
        win_rate = wins / n * 100 if n else 0
        total_pnl = sum(t["pnl"] for t in trades)
        mean_pnl = int(statistics.mean(t["pnl"] for t in trades)) if trades else 0

        # Bug detection
        bugs = _detect_bugs(strat, trades)

        # Monte Carlo
        rand_wins = _random_baseline(
            bars_by_sym, n, tp, sl,
            simulations=args.simulations, rng=rng,
        )
        if rand_wins:
            rand_mean = statistics.mean(rand_wins)
            rand_max = max(rand_wins)
            below = sum(1 for w in rand_wins if w < win_rate)
            pctile = below / len(rand_wins) * 100
        else:
            rand_mean = rand_max = pctile = 0

        # Wilson CI
        ci = wilson_ci(wins, n) if n > 0 else None
        ci_lower = ci.lower * 100 if ci else 0
        ci_upper = ci.upper * 100 if ci else 0

        # Walk-forward — chunk by year
        chunks: list[list[dict]] = [[] for _ in range(6)]
        if all_bars:
            ts_min = all_bars[0].timestamp
            total_secs = (all_bars[-1].timestamp - ts_min).total_seconds()
            chunk_secs = total_secs / 6 if total_secs > 0 else 1
            for t in trades:
                idx = min(5, int((t["entry_ts"] - ts_min).total_seconds()
                                 / chunk_secs))
                if idx >= 0:
                    chunks[idx].append(t)
        chunk_wins = []
        for ch in chunks:
            if len(ch) >= 3:
                cwins = sum(1 for t in ch if t["pnl"] > 0)
                chunk_wins.append(cwins / len(ch) * 100)
        if len(chunk_wins) >= 3:
            cmean = statistics.mean(chunk_wins)
            cstd = statistics.stdev(chunk_wins)
            cov = cstd / cmean if cmean > 0 else 1
        else:
            cmean = cstd = cov = None

        # Grade
        grade_reasons = []
        if bugs:
            grade_reasons.append(f"버그의심: {', '.join(bugs)}")
            grade = "D"
        elif n < 10:
            grade = "C"
            grade_reasons.append(f"n={n} 부족")
        elif pctile < 25:
            grade = "D"
            grade_reasons.append(f"random {pctile:.0f}%ile (미만)")
        elif pctile >= 95 and cov is not None and cov < 0.3 and ci_lower > 50:
            grade = "A"
            grade_reasons.append(
                f"Monte Carlo {pctile:.0f}%ile + COV {cov:.2f} + CI {ci_lower:.0f}% above 50",
            )
        elif pctile >= 75 and (cov is None or cov < 0.5):
            grade = "B"
            grade_reasons.append(f"MC {pctile:.0f}%ile, COV {cov if cov else '?'}")
        elif pctile >= 50:
            grade = "C"
            grade_reasons.append(f"MC {pctile:.0f}%ile (random 동등)")
        else:
            grade = "D"
            grade_reasons.append(f"MC {pctile:.0f}%ile (random 미만)")

        rows.append({
            "strat": strat, "kr": _KR.get(strat, strat),
            "n": n, "win_rate": win_rate, "total_pnl": total_pnl,
            "mean_pnl": mean_pnl,
            "ci_lower": ci_lower, "ci_upper": ci_upper,
            "rand_mean": rand_mean, "rand_max": rand_max, "pctile": pctile,
            "cov": cov, "chunks_fired": len(chunk_wins),
            "bugs": bugs, "grade": grade, "reasons": grade_reasons,
        })

    # --- Sort by grade then by total_pnl ---
    grade_order = {"A": 0, "B": 1, "C": 2, "D": 3}
    rows.sort(key=lambda r: (grade_order.get(r["grade"], 4), -r["total_pnl"]))

    # --- Write markdown report ---
    md = Path(args.output)
    out = []
    out.append("# Strategy 신뢰성 종합 검증 보고서")
    out.append("")
    out.append(f"생성: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    out.append(f"데이터: 일봉 {args.days}일 x top {args.top} 종목, "
               f"{len(all_bars):,} bars, Monte Carlo {args.simulations} sims")
    out.append("")
    out.append("## 검증 5단계")
    out.append("")
    out.append("1. Daily backtest (696일, 13 strategies + detector pre-feed)")
    out.append("2. Monte Carlo random baseline percentile")
    out.append("3. Walk-forward 6 chunks COV")
    out.append("4. Wilson CI (binomial 95%)")
    out.append("5. **버그 자동 탐지**: extreme_move (>30% pnl) / too_perfect "
               "(win>95% n>=5) / multi_entry_same_min / weird_holding_time")
    out.append("")
    out.append("## 신뢰 등급 기준")
    out.append("")
    out.append("- **A 신뢰**: 버그 없음 + Monte Carlo >= 95%ile + Walk-forward "
               "COV < 0.3 + Wilson CI lower > 50%")
    out.append("- **B 통과**: 버그 없음 + MC >= 75%ile + COV < 0.5")
    out.append("- **C 의심**: 통계 부족 (n<10) 또는 MC 50-75%ile")
    out.append("- **D 비활성**: 버그 의심 또는 MC < 25%ile (random 미만)")
    out.append("")
    out.append("## 종합 결과")
    out.append("")
    out.append("| 등급 | 전략 | n | win% | MC%ile | CI[L,U] | COV | total_pnl | 버그 | 사유 |")
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        cov_s = f"{r['cov']:.2f}" if r['cov'] is not None else "?"
        bugs_s = "; ".join(r["bugs"]) if r["bugs"] else "-"
        out.append(
            f"| **{r['grade']}** | {r['kr']} | {r['n']} | {r['win_rate']:.1f}% | "
            f"{r['pctile']:.0f}% | [{r['ci_lower']:.0f},{r['ci_upper']:.0f}] | "
            f"{cov_s} | {r['total_pnl']:+,} | {bugs_s} | {'; '.join(r['reasons'])} |",
        )
    out.append("")
    out.append("## 분류 summary")
    out.append("")
    for grade in ("A", "B", "C", "D"):
        ss = [r for r in rows if r["grade"] == grade]
        if ss:
            out.append(f"- **{grade}** ({len(ss)}개): "
                       f"{', '.join(r['kr'] for r in ss)}")
    out.append("")
    out.append("## 현금 매매 권고")
    out.append("")
    a_strats = [r["kr"] for r in rows if r["grade"] == "A"]
    b_strats = [r["kr"] for r in rows if r["grade"] == "B"]
    if a_strats:
        out.append(f"- **현금 매매 가능 (A 등급, {len(a_strats)}개)**: "
                   f"{', '.join(a_strats)}")
    else:
        out.append("- ⚠️ **A 등급 strategy 없음** — 모두 추가 검증 필요")
    if b_strats:
        out.append(f"- 추가 검증 후 가능 (B 등급, {len(b_strats)}개): "
                   f"{', '.join(b_strats)}")
    out.append("")
    out.append("## 분봉 strategies (이 보고서 미포함)")
    out.append("")
    out.append("BNF이격도 / 변동성돌파(분봉) / VWAP / 시초 / 듀얼 / 외국인수급 / "
               "tape_burst / closing_bet — 분봉 데이터 필요, scripts/"
               "monte_carlo_minute_strategies.py 결과 별도 보고서 (cycle 39).")

    md.write_text("\n".join(out), encoding="utf-8")
    print(f"\n✓ Report written: {md}")
    print(f"  A grade: {sum(1 for r in rows if r['grade']=='A')}")
    print(f"  B grade: {sum(1 for r in rows if r['grade']=='B')}")
    print(f"  C grade: {sum(1 for r in rows if r['grade']=='C')}")
    print(f"  D grade: {sum(1 for r in rows if r['grade']=='D')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
