"""backtest_foreign_flow — 중기 ForeignFlowStrategy 단독 backtest.

사용자 룰 (2026-05-15) 옵션 B (proxy) — KIS mock historical 한계 우회.

데이터 source 두 가지:
- ``--proxy``: 일봉 데이터 (BarStore 1d) 의 거래대금 spike + 가격 +N% 를
  외인 매수 spike proxy 로 inject. 13개월 전체 가능.
- ``--real``: data/foreign_flow.sqlite (KIS mock fetch 결과, 30일) 사용.

ForeignFlowStrategy 가 ForeignNetBuy event 받고 다음 tick (= 다음 일봉 close)
에서 BUY signal emit → TickReplayDriver fill.

CSV: trades / summary / period (mid_term = 월별).
"""

from __future__ import annotations

import argparse
import csv
import logging
import statistics
import sqlite3
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ks_ws.backtest.tick_replay import TickReplayDriver
from ks_ws.bus import EventBus
from ks_ws.domain import Tick
from ks_ws.events import ForeignNetBuy
from ks_ws.sources.atr_provider import BarStoreATRProvider
from ks_ws.sources.foreign_flow_proxy import ProxyConfig, emit_proxy_events_from_bars
from ks_ws.storage.bars import BarStore
from ks_ws.storage.universe import UniverseRegistry
from ks_ws.strategies.foreign_flow_strategy import ForeignFlowStrategy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backtest_foreign_flow")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--top", type=int, default=200, help="universe top N (0=all)")
    p.add_argument("--days", type=int, default=400, help="lookback days")
    p.add_argument("--proxy", action="store_true", help="use proxy events (default)")
    p.add_argument("--real", action="store_true",
                   help="use real KIS data from data/foreign_flow.sqlite (overrides --proxy)")
    p.add_argument("--strong-threshold-krw", type=int, default=100_000_000_000,
                   help="ForeignFlowStrategy strong_threshold_krw (default 1000억)")
    p.add_argument("--csv-out-prefix", type=str, default="data/reports/foreign_flow_backtest")
    p.add_argument("--proxy-vol-ratio", type=float, default=1.5)
    p.add_argument("--proxy-price-pct", type=float, default=3.0)
    p.add_argument("--proxy-foreign-share", type=float, default=0.30)
    args = p.parse_args()

    bar_store = BarStore("data")
    reg = UniverseRegistry("data/universe.sqlite")
    if args.top <= 0:
        universe = reg.top_by_market_cap(100_000)
    else:
        universe = reg.top_by_market_cap(args.top)
    codes = [e.code for e in universe]
    reg.close()

    print(f"\n=== backtest_foreign_flow | universe {len(codes)} | "
          f"days {args.days} | mode={'real' if args.real else 'proxy'} ===\n")

    # 1. Load daily bars per symbol
    all_bars = []
    for sym in codes:
        bars = list(bar_store.read(sym, "1d"))
        if len(bars) >= args.days:
            all_bars.extend(bars[-args.days:])
        elif bars:
            all_bars.extend(bars)
    log.info("loaded %d daily bars", len(all_bars))

    # 2. Strategy + bus
    bus = EventBus()
    atr_d = BarStoreATRProvider(bar_store, timeframe="1d", period=14, ttl_seconds=3600)
    strategy = ForeignFlowStrategy(
        watchlist=set(codes),
        strong_threshold_krw=args.strong_threshold_krw,
        take_profit_pct=20.0, stop_loss_pct=8.0,
        max_hold_minutes=60 * 24 * 30,  # 30일
        confidence=0.6,
        atr_provider=atr_d,
    )

    # 3. Build event source
    captured_events: list[ForeignNetBuy] = []
    if args.real:
        # Read from sqlite
        path = "data/foreign_flow.sqlite"
        conn = sqlite3.connect(path)
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT symbol, date, net_buy_krw FROM foreign_flow ORDER BY date, symbol"
        ).fetchall()
        conn.close()
        for sym, date_str, net_krw in rows:
            if sym not in set(codes):
                continue
            ts = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=UTC, hour=15, minute=30)
            captured_events.append(
                ForeignNetBuy(symbol=sym, timestamp=ts,
                              delta_krw=int(net_krw), window_seconds=86_400)
            )
        log.info("real KIS events loaded: %d", len(captured_events))
    else:
        # Proxy
        cfg = ProxyConfig(
            volume_ratio=args.proxy_vol_ratio,
            price_jump_pct=args.proxy_price_pct,
            foreign_share=args.proxy_foreign_share,
        )

        # Capture into list via tmp bus
        tmp_bus = EventBus(default_maxsize=2_000_000)
        sub = tmp_bus.subscribe(ForeignNetBuy, maxsize=2_000_000)
        n = emit_proxy_events_from_bars(all_bars, tmp_bus, config=cfg, lookback=5)
        log.info("proxy events emitted: %d", n)
        while sub.qsize() > 0:
            captured_events.append(sub.get_nowait())

    # 4. Build replay items: bars (for ATR + TP/SL price) + ticks (for entry/exit) + events
    items: list = []
    for bar in all_bars:
        items.append(bar)
        items.append(Tick(
            symbol=bar.symbol, timestamp=bar.timestamp,
            price=bar.close, volume=bar.volume,
        ))
    items.extend(captured_events)
    items.sort(key=lambda x: x.timestamp)
    log.info("total replay items: %d", len(items))

    # 5. Run replay
    started = datetime.now(UTC)
    with TickReplayDriver(items, [strategy]) as driver:
        result = driver.run()
    elapsed = (datetime.now(UTC) - started).total_seconds()
    log.info("replay done in %.1fs — %d intents, %d fills",
             elapsed, result.total_intents, len(result.fills))

    # 6. Aggregate per-symbol
    by_strat: dict[str, list[tuple]] = defaultdict(list)
    for intent, fill_price in result.fills:
        src = intent.sources[0] if intent.sources else "?"
        by_strat[src].append((intent.symbol, intent.side.value,
                              fill_price, intent.timestamp))

    print(f"\n=== ForeignFlow ({'real' if args.real else 'proxy'}) 결과 ===")
    for strat, trades in by_strat.items():
        positions: dict[str, list] = defaultdict(list)
        pnls = []
        for sym, side, price, _ts in trades:
            if side == "buy":
                positions[sym].append(price)
            elif side == "sell" and positions[sym]:
                pnls.append(price - positions[sym].pop(0))
        if pnls:
            wins = sum(1 for p in pnls if p > 0)
            losses = sum(1 for p in pnls if p < 0)
            wr = wins / len(pnls) * 100
            print(f"  {strat}: n={len(pnls)} win={wins} loss={losses} "
                  f"win%={wr:.1f}% mean={int(statistics.mean(pnls)):+,} "
                  f"total={sum(pnls):+,}")
        else:
            print(f"  {strat}: 0 closed trades")

    # 7. CSV emit
    if args.csv_out_prefix:
        suffix = "_real" if args.real else "_proxy"
        trades_path = f"{args.csv_out_prefix}{suffix}_trades.csv"
        summary_path = f"{args.csv_out_prefix}{suffix}_summary.csv"
        period_path = f"{args.csv_out_prefix}{suffix}_period.csv"
        Path(trades_path).parent.mkdir(parents=True, exist_ok=True)

        with open(trades_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["strategy", "style", "symbol", "trade_seq",
                        "entry_ts", "entry_price", "exit_ts", "exit_price",
                        "pnl_krw", "pnl_pct", "hold_minutes"])
            for strat, trades in by_strat.items():
                positions: dict[str, list[tuple]] = defaultdict(list)
                seq: dict[str, int] = defaultdict(int)
                for sym, side, price, ts in trades:
                    if side == "buy":
                        positions[sym].append((price, ts))
                    elif side == "sell" and positions[sym]:
                        e_price, e_ts = positions[sym].pop(0)
                        seq[sym] += 1
                        pnl = price - e_price
                        pnl_pct = (pnl / e_price * 100) if e_price else 0
                        hold_min = int((ts - e_ts).total_seconds() / 60)
                        w.writerow([strat, "mid_term", sym, seq[sym],
                                    e_ts.isoformat(), e_price,
                                    ts.isoformat(), price,
                                    pnl, f"{pnl_pct:.4f}", hold_min])

        with open(summary_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["strategy", "style", "symbol", "n_trades", "wins",
                        "losses", "win_rate_pct", "total_pnl_krw",
                        "mean_pnl_krw", "best_pnl", "worst_pnl"])
            for strat, trades in by_strat.items():
                per_sym: dict[str, list[int]] = defaultdict(list)
                positions: dict[str, list[int]] = defaultdict(list)
                for sym, side, price, _ts in trades:
                    if side == "buy":
                        positions[sym].append(price)
                    elif side == "sell" and positions[sym]:
                        per_sym[sym].append(price - positions[sym].pop(0))
                for sym, pnls in per_sym.items():
                    wins = sum(1 for p in pnls if p > 0)
                    losses = sum(1 for p in pnls if p < 0)
                    wr = wins / len(pnls) * 100 if pnls else 0
                    w.writerow([strat, "mid_term", sym, len(pnls), wins, losses,
                                f"{wr:.2f}", sum(pnls),
                                int(statistics.mean(pnls)) if pnls else 0,
                                max(pnls) if pnls else 0,
                                min(pnls) if pnls else 0])

        # 기간별 = 중기 → 월별
        with open(period_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["strategy", "style", "symbol", "period_type",
                        "period_label", "n_trades", "wins", "losses",
                        "total_pnl_krw", "mean_pnl_krw"])
            for strat, trades in by_strat.items():
                positions: dict[str, list[tuple]] = defaultdict(list)
                bucket: dict = defaultdict(list)
                for sym, side, price, ts in trades:
                    if side == "buy":
                        positions[sym].append((price, ts))
                    elif side == "sell" and positions[sym]:
                        e_price, e_ts = positions[sym].pop(0)
                        label = e_ts.strftime("%Y-%m")
                        bucket[(sym, label)].append(price - e_price)
                for (sym, label), pnls in sorted(bucket.items()):
                    wins = sum(1 for p in pnls if p > 0)
                    losses = sum(1 for p in pnls if p < 0)
                    w.writerow([strat, "mid_term", sym, "monthly", label,
                                len(pnls), wins, losses, sum(pnls),
                                int(statistics.mean(pnls)) if pnls else 0])

        print(f"\n[CSV] {trades_path}")
        print(f"[CSV] {summary_path}")
        print(f"[CSV] {period_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
