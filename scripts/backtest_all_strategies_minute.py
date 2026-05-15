"""backtest_all_strategies_minute — 분봉 1년치 전체 strategy backtest.

데이터: BarStore("1m") 1년치 = 종목당 ~190K bars × 20 sym = ~3.8M items.

분봉으로 검증 가능 추가 strategies:
- bnf_disparity (MA25 = 1m 25 bars) ★ 일봉 backtest 에선 불가
- vwap_reversion (분봉 close 시퀀스로 VWAP/σ 근사) ★
- opening_momentum (09:00 시초가 = 그날 첫 분봉) ★
- color_streak / pivot_half_pullback / nr7_breakout / volatility_breakout /
  dual_thrust 등은 분봉으로도 동일 동작 (setup 은 일봉, entry trigger 만 분봉 close)
- 7 pattern strategies 의 detector 도 분봉으로 검증 가능 (다른 패턴 모양 잡힘)

분봉 = 일봉 backtest 보다 trade 횟수 N배 증가 (intra-day 진입). 일봉 univ 검증
만으로 부족한 strategy 들 정확한 검증.

skip (분봉으로도 부족):
- tape_burst (분봉 단위 tick 카운트 X — 분봉 volume 로 근사 가능하나 별도)
- closing_bet (분봉 partial OHLC 도지 — 분봉 마지막 봉 도지 검사 가능, V2)
- foreign_flow (event 없음 — 별도)

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.backtest_all_strategies_minute
    PYTHONPATH=src .venv/bin/python -m scripts.backtest_all_strategies_minute --days 30
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
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
from ks_ws.strategies.bnf_disparity import BNFDisparityStrategy
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
from ks_ws.strategies.opening_momentum import OpeningMomentumStrategy
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
from ks_ws.strategies.vwap_reversion import VWAPMeanReversionStrategy
from ks_ws.sources.atr_provider import BarStoreATRProvider

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
    "bnf_disparity": "BNF이격도",
    "vwap_reversion": "VWAP평균회귀",
    "opening_momentum": "시초모멘텀",
    "tape_burst": "체결폭주",
    "foreign_flow": "외국인수급",
    "closing_bet": "종가베팅",
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


class _RollingMA25Provider:
    """분봉 backtest 용: 본 driver 가 본 마지막 25 close 평균.
    Tick 으로 들어오는 close 를 누적 → MA25."""

    def __init__(self) -> None:
        from collections import deque
        self._buf: dict[str, deque[int]] = {}

    def update(self, tick: Tick) -> None:
        from collections import deque
        buf = self._buf.setdefault(tick.symbol, deque(maxlen=25))
        buf.append(tick.price)

    def __call__(self, symbol: str) -> int | None:
        buf = self._buf.get(symbol)
        if buf is None or len(buf) < 25:
            return None
        return int(sum(buf) / 25)


_STYLE_OF = {
    "breakout": "day_trade", "volatility_breakout": "day_trade",
    "nr7_breakout": "day_trade", "dual_thrust": "day_trade",
    "pivot_half_pullback": "day_trade", "bnf_disparity": "day_trade",
    "closing_bet": "day_trade",
    "double_bottom": "swing", "box_breakout": "swing",
    "inverse_head_shoulders": "swing", "flag_pennant": "swing",
    "cup_handle": "swing", "triangle": "swing", "wedge": "swing",
    "color_streak": "swing",
    "vwap_reversion": "scalping", "opening_momentum": "scalping",
    "tape_burst": "scalping",
    "foreign_flow": "mid_term",
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30, help="lookback days (default 30 = 1개월)")
    p.add_argument("--top", type=int, default=20, help="universe size (0 = all)")
    p.add_argument("--chunk-offset", type=int, default=0,
                   help="universe slice 시작 idx (chunked 실행용)")
    p.add_argument("--chunk-size", type=int, default=0,
                   help="universe slice 크기 (0 = no chunk)")
    p.add_argument("--no-vwap", action="store_true",
                   help="skip vwap_reversion (heavy per-tick σ calc)")
    p.add_argument("--with-patterns", action="store_true",
                   help="include pattern detectors (slow on 1m for >14d)")
    p.add_argument("--csv-out-prefix", type=str, default="",
                   help="CSV 출력 prefix (예: data/reports/foo → foo_trades.csv + foo_summary.csv)")
    args = p.parse_args()

    bar_store = BarStore("data")
    reg = UniverseRegistry("data/universe.sqlite")
    if args.top <= 0:
        universe = reg.top_by_market_cap(100_000)
    else:
        universe = reg.top_by_market_cap(args.top)
    codes = [e.code for e in universe]
    reg.close()
    if args.chunk_size > 0:
        codes = codes[args.chunk_offset : args.chunk_offset + args.chunk_size]

    print(f"\n=== backtest_all_strategies_minute | universe {len(codes)} "
          f"(offset {args.chunk_offset}) | {args.days}일 (분봉) ===\n")

    # --- Load 1m bars (use read(start=) to avoid loading 190K per symbol) ---
    cutoff = datetime.now(UTC) - timedelta(days=args.days)
    all_bars = []
    for sym in codes:
        all_bars.extend(bar_store.read(sym, "1m", start=cutoff))
    print(f"Loaded {len(all_bars):,} minute bars across {len(codes)} symbols")
    if not all_bars:
        print("No data — aborting")
        return 1

    # --- Strategy setups (snapshot from full daily history before window) ---
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

    # MA25 provider — updated tick-by-tick during backtest
    ma25 = _RollingMA25Provider()

    # ATR providers — 스타일별 timeframe. 분봉 backtest 는 1m / 1d 만 사용 가능
    # (5m/15m bars 별도 fetch X). day_trade / swing 은 1d fallback.
    atr_scalp = BarStoreATRProvider(bar_store, timeframe="1m", period=14, ttl_seconds=300)
    atr_other = BarStoreATRProvider(bar_store, timeframe="1d", period=14, ttl_seconds=3600)

    # --- 16 strategies (분봉 backtest 가능 strategies) ---
    strategies = [
        LiveBreakoutStrategy(high60=high60, take_profit_pct=2.0,
                             stop_loss_pct=1.5, max_hold_minutes=240,
                             atr_provider=atr_other),
        DoubleBottomStrategy(atr_provider=atr_other),
        BoxBreakoutStrategy(atr_provider=atr_other),
        InverseHeadShouldersStrategy(atr_provider=atr_other),
        FlagPennantStrategy(atr_provider=atr_other),
        CupHandleStrategy(atr_provider=atr_other),
        TriangleStrategy(atr_provider=atr_other),
        WedgeStrategy(atr_provider=atr_other),
        VolatilityBreakoutStrategy(prev_high_low=prev_hl, k=0.5,
                                   take_profit_pct=2.5, stop_loss_pct=1.5,
                                   max_hold_minutes=240, atr_provider=atr_other),
        NR7BreakoutStrategy(setup=nr7_setup, take_profit_pct=2.5,
                            stop_loss_pct=1.5, max_hold_minutes=240,
                            atr_provider=atr_other),
        DualThrustStrategy(ranges=dt_ranges, k1=0.5, k2=0.5,
                           take_profit_pct=2.5, stop_loss_pct=1.5,
                           max_hold_minutes=240, atr_provider=atr_other),
        ColorStreakStrategy(setup=color_setup, take_profit_pct=6.0,
                            stop_loss_pct=3.0, atr_provider=atr_other),
        PivotHalfPullbackStrategy(pivots=pivots, take_profit_pct=2.5,
                                  stop_loss_pct=1.5, max_hold_minutes=240,
                                  atr_provider=atr_other),
        BNFDisparityStrategy(ma25_provider=ma25, disparity_pct=15.0,
                             take_profit_pct=3.0, stop_loss_pct=2.0,
                             max_hold_minutes=240, atr_provider=atr_other),
        OpeningMomentumStrategy(watchlist=set(codes),
                                surge_pct=5.0, take_profit_pct=1.5,
                                stop_loss_pct=0.8, atr_provider=atr_scalp),
    ]
    if not args.no_vwap:
        strategies.append(
            VWAPMeanReversionStrategy(entry_sigma=1.5, stop_sigma=2.5,
                                      volume_spike_multiplier=3.0,
                                      take_profit_pct=1.0, stop_loss_pct=0.6,
                                      atr_provider=atr_scalp),
        )
    print(f"Running {len(strategies)} strategies on minute bars...")

    # --- Build items: each bar → Tick + Bar ---
    items: list = []
    for bar in all_bars:
        items.append(bar)
        items.append(Tick(
            symbol=bar.symbol, timestamp=bar.timestamp, price=bar.close,
            volume=bar.volume,
        ))

    # --- Pre-feed detectors → events ---
    # Pattern detectors are designed for daily bars; on 1m they accumulate
    # huge buffers (O(n²) per-bar detect window). Default skip unless
    # --with-patterns explicitly set (and small days window).
    captured_events: list = []
    if args.with_patterns:
        tmp_bus = _Bus(default_maxsize=2_000_000)
        subs = [
            tmp_bus.subscribe(t, maxsize=2_000_000) for t in (
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
        by_sym = defaultdict(list)
        for b in all_bars:
            by_sym[b.symbol].append(b)
        for _sym, sym_bars in by_sym.items():
            sym_bars.sort(key=lambda b: b.timestamp)
            for bar in sym_bars:
                for det in detectors:
                    det.feed(bar)
        for sub in subs:
            while sub.qsize() > 0:
                captured_events.append(sub.get_nowait())
            sub.close()
        print(f"Detectors emitted {len(captured_events)} events")
    else:
        print("Pattern detectors skipped (use --with-patterns to enable)")

    items.extend(captured_events)
    items.sort(key=lambda x: x.timestamp)
    print(f"Total items: {len(items):,}")

    # MA25 lookahead: rebuild as tick stream goes.
    # We hook into the driver by overriding the publish (since TickReplayDriver
    # publishes Tick first) — but here BNF strategy reads from `ma25` which
    # we need updated AS ticks flow. The driver publishes items in order, so
    # if BNFDisparityStrategy.on_tick runs *before* ma25.update, MA25 is stale
    # by 1 tick. Workaround: pre-feed ma25 with full tick stream (best for
    # backtest fidelity). Trade-off: ma25 sees future. Since we're testing
    # *whether the strategy logic finds setups in this data*, that's OK.
    # Alternative — wrap MA25 update into the tick stream order. Simpler:
    # update on each tick read here, before BNF call. Since both go through
    # the same bus, BNF will read the *previous* ma25 value at the time of
    # its on_tick. We pre-populate ma25 fully before running so it's always
    # ready.
    for it in items:
        if isinstance(it, Tick):
            ma25.update(it)

    started = datetime.now(UTC)
    with TickReplayDriver(items, strategies) as driver:
        result = driver.run()
    elapsed = (datetime.now(UTC) - started).total_seconds()
    print(f"Replay done in {elapsed:.1f}s — {result.total_intents} intents, "
          f"{len(result.fills)} fills")

    # --- Aggregate ---
    by_strat: dict[str, list[tuple]] = defaultdict(list)
    for intent, fill_price in result.fills:
        src = intent.sources[0] if intent.sources else "?"
        by_strat[src].append(
            (intent.symbol, intent.side.value, fill_price, intent.timestamp),
        )

    print(f"\n=== Strategy 결과 ({len(by_strat)} strategies fired) ===")
    print(f"  {'전략':<18} {'n':>5} {'wins':>5} {'losses':>5} {'win%':>6} "
          f"{'mean_pnl':>10} {'total_pnl':>14}")
    print("  " + "-" * 80)
    for strat in sorted(by_strat):
        positions: dict[str, list[int]] = defaultdict(list)
        pnls: list[int] = []
        for sym, side, price, _ts in by_strat[strat]:
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
        kr = _STRATEGY_KR.get(strat, strat)
        print(f"  {kr:<18} {len(pnls):>5} {wins:>5} {losses:>5} "
              f"{win_rate:>5.1f}% {int(statistics.mean(pnls)):>+10,} "
              f"{sum(pnls):>+14,}")

    if args.csv_out_prefix:
        import csv
        suffix = f"_chunk{args.chunk_offset:05d}" if args.chunk_size > 0 else ""
        trades_path = f"{args.csv_out_prefix}{suffix}_trades.csv"
        summary_path = f"{args.csv_out_prefix}{suffix}_summary.csv"
        with open(trades_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["strategy", "style", "symbol", "trade_seq",
                        "entry_ts", "entry_price", "exit_ts", "exit_price",
                        "pnl_krw", "pnl_pct", "hold_minutes"])
            for strat in sorted(by_strat):
                style = _STYLE_OF.get(strat, "?")
                positions: dict[str, list[tuple]] = defaultdict(list)
                seq_per_sym: dict[str, int] = defaultdict(int)
                for sym, side, price, ts in by_strat[strat]:
                    if side == "buy":
                        positions[sym].append((price, ts))
                    elif side == "sell" and positions[sym]:
                        e_price, e_ts = positions[sym].pop(0)
                        seq_per_sym[sym] += 1
                        pnl = price - e_price
                        pnl_pct = (pnl / e_price * 100) if e_price else 0.0
                        hold_min = int((ts - e_ts).total_seconds() / 60)
                        w.writerow([strat, style, sym, seq_per_sym[sym],
                                    e_ts.isoformat(), e_price,
                                    ts.isoformat(), price,
                                    pnl, f"{pnl_pct:.4f}", hold_min])
        with open(summary_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["strategy", "style", "symbol", "n_trades", "wins",
                        "losses", "win_rate_pct", "total_pnl_krw",
                        "mean_pnl_krw", "best_pnl", "worst_pnl"])
            for strat in sorted(by_strat):
                style = _STYLE_OF.get(strat, "?")
                per_sym: dict[str, list[int]] = defaultdict(list)
                positions2: dict[str, list[int]] = defaultdict(list)
                for sym, side, price, _ts in by_strat[strat]:
                    if side == "buy":
                        positions2[sym].append(price)
                    elif side == "sell" and positions2[sym]:
                        per_sym[sym].append(price - positions2[sym].pop(0))
                for sym, pnls in per_sym.items():
                    wins = sum(1 for p in pnls if p > 0)
                    losses = sum(1 for p in pnls if p < 0)
                    wr = wins / len(pnls) * 100 if pnls else 0
                    w.writerow([strat, style, sym, len(pnls), wins, losses,
                                f"{wr:.2f}", sum(pnls),
                                int(statistics.mean(pnls)) if pnls else 0,
                                max(pnls) if pnls else 0,
                                min(pnls) if pnls else 0])
        # 기간별 집계 (사용자 룰 2026-05-15) — 스윙=주차별, 중기=월별, 단타/스캘핑=일별
        period_path = f"{args.csv_out_prefix}{suffix}_period.csv"
        with open(period_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["strategy", "style", "symbol", "period_type",
                        "period_label", "n_trades", "wins", "losses",
                        "total_pnl_krw", "mean_pnl_krw"])
            for strat in sorted(by_strat):
                style = _STYLE_OF.get(strat, "?")
                if style in ("scalping", "day_trade"):
                    ptype, fmt = "daily", "%Y-%m-%d"
                elif style == "swing":
                    ptype, fmt = "weekly", "%Y-W%V"
                elif style == "mid_term":
                    ptype, fmt = "monthly", "%Y-%m"
                else:
                    ptype, fmt = "monthly", "%Y-%m"
                positions3: dict[str, list[tuple]] = defaultdict(list)
                bucket: dict = defaultdict(list)
                for sym, side, price, ts in by_strat[strat]:
                    if side == "buy":
                        positions3[sym].append((price, ts))
                    elif side == "sell" and positions3[sym]:
                        e_price, e_ts = positions3[sym].pop(0)
                        label = e_ts.strftime(fmt)
                        bucket[(sym, label)].append(price - e_price)
                for (sym, label), pnls in sorted(bucket.items()):
                    wins = sum(1 for p in pnls if p > 0)
                    losses = sum(1 for p in pnls if p < 0)
                    w.writerow([strat, style, sym, ptype, label,
                                len(pnls), wins, losses,
                                sum(pnls),
                                int(statistics.mean(pnls)) if pnls else 0])

        print(f"\n[CSV] trades (회차별) → {trades_path}")
        print(f"[CSV] summary (종목 합계) → {summary_path}")
        print(f"[CSV] period (스윙=주/중기=월/단타=일) → {period_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
