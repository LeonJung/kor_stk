"""backtest_vb_compare — 변동성돌파 (vb) V1 vs V2 backtest 비교.

목적:
  - V1 (개선 전 baseline): trailing 비활성 + sector blacklist 미적용 + symbol
    weight 미적용. 5/15 commit 76e4660 이전 동작 재현.
  - V2 (Tier 1+3+5 적용 후): sector blacklist universe filter + anchor-based
    trailing (activation 1.5% / trail 1.0%) + SymbolWeightMatrix (Allocator
    BUY magnitude scaling, weight=0 = 차단).

전체 universe `reg.top_by_market_cap(100_000)` × 가능한 최대 분봉 기간.
메모리 한도 (15GB) 고려해 chunk_size 단위로 잘라서 누적 backtest.

V1 / V2 결과 trade CSV → 비교 보고서 (월별 PnL diff / top winner/loser /
승률 / MDD / 월별 흑자 비율) → markdown report.md 출력.

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.backtest_vb_compare \\
        --days 400 --chunk-size 50

옵션:
    --days N         lookback 일수 (default 400)
    --chunk-size N   universe 분할 크기 (default 50)
    --top N          universe top-N (default 0 = all top_by_market_cap)
    --out-dir PATH   결과 디렉터리 (default data/reports/vb_compare_YYYYMMDD)
    --resume         이미 처리된 chunk 는 skip (CSV 존재)
"""

from __future__ import annotations

import argparse
import csv
import gc
import statistics
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ks_ws.backtest.tick_replay import TickReplayDriver
from ks_ws.domain import Tick
from ks_ws.sources.atr_provider import BarStoreATRProvider
from ks_ws.sources.entry_gate import EntryGate, EntryGateConfig
from ks_ws.sources.jab_filter import JabFilter, JabFilterRules, load_krx_alert_codes
from ks_ws.sources.sector_blacklist import filter_universe, load_name_map
from ks_ws.sources.volume_filter import VolumeFilter
from ks_ws.storage.bars import BarStore
from ks_ws.storage.universe import UniverseRegistry
from ks_ws.strategies.allocator import Allocator
from ks_ws.strategies.symbol_weights import SymbolWeightMatrix
from ks_ws.strategies.vb_scalein import VBScaleInStrategy
from ks_ws.strategies.volatility_breakout import (
    VolatilityBreakoutStrategy,
    compute_prev_close,
    compute_prev_high_low,
)

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"


# ---------- Per-chunk backtest helpers ----------


def _build_items(bar_store: BarStore, codes: list[str], cutoff: datetime) -> list:
    """Load 1m bars → emit Bar + Tick. Bar 가 volume_filter 의 on_bar 갱신용."""
    items: list = []
    for sym in codes:
        for bar in bar_store.read(sym, "1m", start=cutoff):
            items.append(bar)
            items.append(Tick(
                symbol=bar.symbol,
                timestamp=bar.timestamp,
                price=bar.close,
                volume=bar.volume,
            ))
    return items


def _run_one(
    *,
    mode: str,
    codes: list[str],
    bar_store: BarStore,
    items: list,
    prev_hl: dict[str, tuple[int, int]],
    atr_provider,
    symbol_weights: SymbolWeightMatrix | None,
    volume_filter: VolumeFilter | None = None,
    stop_loss_pct: float = 1.5,
    take_profit_pct: float = 0.0,  # 0 = SL × 2 자동 (사용자 룰)
    max_hold_minutes: int = 60,
    entry_gate: EntryGate | None = None,
    prev_close: dict[str, int] | None = None,
    daily_history: dict[str, list] | None = None,
) -> tuple[list[tuple], int]:
    """Run a single backtest (V1 or V2) on the given items.

    Returns:
        ([(symbol, side, fill_price, ts, sources)], n_intents)
    """
    # 사용자 룰 (5/18): TP 미지정 시 SL × 2 자동 (단타 R/R 2:1 default)
    tp_pct = take_profit_pct if take_profit_pct > 0 else stop_loss_pct * 2

    if mode == "v1":
        # V1 baseline — trailing off, no symbol weight, no blacklist filter
        # (caller already passes unfiltered codes).
        strategies = [
            VolatilityBreakoutStrategy(
                prev_high_low=prev_hl,
                k=0.5,
                take_profit_pct=tp_pct,
                stop_loss_pct=stop_loss_pct,
                max_hold_minutes=max_hold_minutes,
                atr_provider=atr_provider,
                trailing_activation_pct=0.0,
                trailing_pct=999.0,
                volume_filter=volume_filter, entry_gate=entry_gate,
                prev_close=prev_close, daily_history=daily_history,
            ),
        ]
        allocator = Allocator(max_position_per_symbol=100)
    elif mode == "v2":
        strategies = [
            VolatilityBreakoutStrategy(
                prev_high_low=prev_hl,
                k=0.5,
                take_profit_pct=tp_pct,
                stop_loss_pct=stop_loss_pct,
                max_hold_minutes=max_hold_minutes,
                atr_provider=atr_provider,
                trailing_activation_pct=1.5,
                trailing_pct=1.0,
                volume_filter=volume_filter, entry_gate=entry_gate,
                prev_close=prev_close, daily_history=daily_history,
            ),
        ]
        allocator = Allocator(
            max_position_per_symbol=100,
            symbol_weights=symbol_weights,
        )
    elif mode == "v3":
        strategies = [
            VBScaleInStrategy(
                prev_high_low=prev_hl,
                k=0.5,
                stop_loss_pct=stop_loss_pct,
                max_hold_minutes=max_hold_minutes,
                trailing_pct=1.0,
                volume_filter=volume_filter, entry_gate=entry_gate,
                prev_close=prev_close, daily_history=daily_history,
            ),
        ]
        allocator = Allocator(
            max_position_per_symbol=100,
            symbol_weights=symbol_weights,
        )
    elif mode == "v4":
        strategies = [
            VolatilityBreakoutStrategy(
                prev_high_low=prev_hl,
                k=0.5,
                take_profit_pct=tp_pct,
                stop_loss_pct=stop_loss_pct,
                max_hold_minutes=max_hold_minutes,
                atr_provider=atr_provider,
                trailing_activation_pct=0.0,
                trailing_pct=999.0,
                volume_filter=volume_filter, entry_gate=entry_gate,
                prev_close=prev_close, daily_history=daily_history,
            ),
        ]
        allocator = Allocator(max_position_per_symbol=100)
    else:
        raise ValueError(f"unknown mode: {mode}")

    with TickReplayDriver(items, strategies, allocator=allocator) as driver:
        result = driver.run()

    fills_out: list[tuple] = []
    for intent, fill_price in result.fills:
        src = intent.sources[0] if intent.sources else "?"
        fills_out.append((
            intent.symbol, intent.side.value, fill_price,
            intent.timestamp, src, intent.quantity,
        ))
    return fills_out, result.total_intents


def _write_chunk_trades(
    csv_path: Path,
    fills_v1: list[tuple], fills_v2: list[tuple], fills_v3: list[tuple],
    fills_v4: list[tuple],
    chunk_offset: int,
) -> tuple[int, int, int, int]:
    """Append v1/v2/v3/v4 trades. V1/V2/V4 = 단일 BUY-SELL FIFO 페어링. V3 =
    round-trip aggregator (부분 매매 weighted avg)."""
    n_v1 = _append_trades(csv_path / "v1_trades.csv", fills_v1, chunk_offset)
    n_v2 = _append_trades(csv_path / "v2_trades.csv", fills_v2, chunk_offset)
    n_v3 = _aggregate_v3_trades(csv_path / "v3_trades.csv", fills_v3, chunk_offset)
    n_v4 = _append_trades(csv_path / "v4_trades.csv", fills_v4, chunk_offset)
    return n_v1, n_v2, n_v3, n_v4


def _append_trades(
    csv_path: Path, fills: list[tuple], chunk_offset: int,
) -> int:
    """Convert buy/sell fills → closed round-trips, append to CSV (V1/V2: 단일
    BUY ↔ 단일 SELL FIFO 페어링). 한 BUY = 한 SELL = 한 row."""
    by_sym: dict[str, list[tuple]] = defaultdict(list)
    for sym, side, price, ts, _src, _qty in fills:
        by_sym[sym].append((side, price, ts))
    positions: dict[str, list[tuple]] = defaultdict(list)
    rows: list[tuple] = []
    for sym, evs in by_sym.items():
        evs.sort(key=lambda x: x[2])
        for side, price, ts in evs:
            if side == "buy":
                positions[sym].append((price, ts))
            elif side == "sell" and positions[sym]:
                e_price, e_ts = positions[sym].pop(0)
                pnl = price - e_price
                pnl_pct = (pnl / e_price * 100) if e_price else 0.0
                hold_min = int((ts - e_ts).total_seconds() / 60)
                rows.append((
                    chunk_offset, sym, e_ts.isoformat(), e_price,
                    ts.isoformat(), price, pnl, f"{pnl_pct:.4f}", hold_min,
                ))
    if not rows:
        return 0
    new_file = not csv_path.exists()
    with open(csv_path, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow([
                "chunk_offset", "symbol", "entry_ts", "entry_price",
                "exit_ts", "exit_price", "pnl_krw", "pnl_pct", "hold_minutes",
            ])
        w.writerows(rows)
    return len(rows)


def _aggregate_v3_trades(
    csv_path: Path, fills: list[tuple], chunk_offset: int,
) -> int:
    """V3 round-trip aggregator — 부분 매매 정확 처리.

    fills = [(sym, side, price, ts, src, qty)]. 한 종목 시간순 BUY/SELL 시퀀스:
    BUY 시 cum_buy_value += price × qty, cum_buy_qty += qty
    SELL 시 cum_exit_value += price × take, cum_exit_qty += take
    cum_exit_qty >= cum_buy_qty 시 청산 완료 → 1 row 생성:
        entry_price = cum_buy_value / cum_buy_qty (가중평균 entry)
        exit_price  = cum_exit_value / cum_exit_qty (가중평균 exit)
        pnl_krw    = (exit_price - entry_price) × cum_buy_qty (총 KRW)
        hold_min   = last_exit_ts - first_buy_ts
    그 후 reset, 다음 BUY 가 새 round 시작.
    미체결 (open) round 는 row 생성 X.
    """
    by_sym: dict[str, list[tuple]] = defaultdict(list)
    for sym, side, price, ts, _src, qty in fills:
        by_sym[sym].append((side, price, ts, qty))

    rows: list[tuple] = []
    for sym, evs in by_sym.items():
        evs.sort(key=lambda x: x[2])
        in_round = False
        round_start_ts = None
        buy_value = 0.0
        buy_qty = 0
        exit_value = 0.0
        exit_qty = 0
        last_exit_ts = None

        for side, price, ts, qty in evs:
            if side == "buy":
                if not in_round:
                    in_round = True
                    round_start_ts = ts
                    buy_value = 0.0
                    buy_qty = 0
                    exit_value = 0.0
                    exit_qty = 0
                    last_exit_ts = None
                buy_value += price * qty
                buy_qty += qty
            else:  # sell
                if not in_round or buy_qty <= 0:
                    continue  # phantom sell (no open round)
                take = min(qty, buy_qty - exit_qty)
                if take <= 0:
                    continue
                exit_value += price * take
                exit_qty += take
                last_exit_ts = ts

                if exit_qty >= buy_qty:
                    avg_entry = buy_value / buy_qty
                    avg_exit = exit_value / exit_qty
                    pnl = int(round((avg_exit - avg_entry) * buy_qty))
                    pnl_pct = (avg_exit / avg_entry - 1) * 100 if avg_entry > 0 else 0.0
                    hold_min = int((last_exit_ts - round_start_ts).total_seconds() / 60)
                    rows.append((
                        chunk_offset, sym, round_start_ts.isoformat(),
                        int(avg_entry), last_exit_ts.isoformat(),
                        int(avg_exit), pnl, f"{pnl_pct:.4f}", hold_min,
                    ))
                    in_round = False
                    round_start_ts = None
                    buy_value = 0.0
                    buy_qty = 0
                    exit_value = 0.0
                    exit_qty = 0
                    last_exit_ts = None

    if not rows:
        return 0
    new_file = not csv_path.exists()
    with open(csv_path, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow([
                "chunk_offset", "symbol", "entry_ts", "entry_price",
                "exit_ts", "exit_price", "pnl_krw", "pnl_pct", "hold_minutes",
            ])
        w.writerows(rows)
    return len(rows)


# ---------- Final aggregation / report ----------


def _read_trades(csv_path: Path) -> list[dict]:
    if not csv_path.exists():
        return []
    rows: list[dict] = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for r in reader:
            r["pnl_krw"] = int(r["pnl_krw"])
            r["pnl_pct"] = float(r["pnl_pct"])
            r["entry_price"] = int(r["entry_price"])
            r["exit_price"] = int(r["exit_price"])
            r["hold_minutes"] = int(r["hold_minutes"])
            rows.append(r)
    return rows


def _monthly_pnl(trades: list[dict]) -> dict[str, dict]:
    """{'YYYY-MM': {'n': N, 'pnl': sum, 'wins': W, 'losses': L}}"""
    out: dict[str, dict] = defaultdict(lambda: {"n": 0, "pnl": 0, "wins": 0, "losses": 0})
    for t in trades:
        month = t["entry_ts"][:7]
        out[month]["n"] += 1
        out[month]["pnl"] += t["pnl_krw"]
        if t["pnl_krw"] > 0:
            out[month]["wins"] += 1
        elif t["pnl_krw"] < 0:
            out[month]["losses"] += 1
    return dict(sorted(out.items()))


def _per_symbol(trades: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = defaultdict(lambda: {"n": 0, "pnl": 0, "wins": 0})
    for t in trades:
        sym = t["symbol"]
        out[sym]["n"] += 1
        out[sym]["pnl"] += t["pnl_krw"]
        if t["pnl_krw"] > 0:
            out[sym]["wins"] += 1
    return out


def _mdd(trades: list[dict]) -> tuple[int, int]:
    """Max drawdown of cumulative PnL (KRW). Returns (mdd, peak)."""
    trades_sorted = sorted(trades, key=lambda t: t["entry_ts"])
    cum = 0
    peak = 0
    mdd = 0
    for t in trades_sorted:
        cum += t["pnl_krw"]
        if cum > peak:
            peak = cum
        dd = cum - peak  # negative or 0
        if dd < mdd:
            mdd = dd
    return mdd, peak


def _name_map_safe() -> dict[str, str]:
    try:
        return load_name_map(str(DATA_ROOT / "universe.sqlite"))
    except Exception:
        return {}


def _build_report(
    v1: list[dict], v2: list[dict], *,
    universe_size: int, v2_universe_size: int,
    days: int, started_at: datetime, finished_at: datetime,
) -> str:
    nm = _name_map_safe()

    def _fmt_krw(n: int) -> str:
        return f"{n:+,}"

    def _summary(rows: list[dict]) -> dict:
        if not rows:
            return {"n": 0, "wins": 0, "losses": 0, "win_pct": 0.0,
                    "mean_pnl": 0, "total_pnl": 0, "mdd": 0}
        wins = sum(1 for r in rows if r["pnl_krw"] > 0)
        losses = sum(1 for r in rows if r["pnl_krw"] < 0)
        mdd, _ = _mdd(rows)
        return {
            "n": len(rows),
            "wins": wins,
            "losses": losses,
            "win_pct": wins / len(rows) * 100,
            "mean_pnl": int(statistics.mean(r["pnl_krw"] for r in rows)),
            "total_pnl": sum(r["pnl_krw"] for r in rows),
            "mdd": mdd,
        }

    s1 = _summary(v1)
    s2 = _summary(v2)
    m1 = _monthly_pnl(v1)
    m2 = _monthly_pnl(v2)

    lines: list[str] = []
    lines.append("# 변동성돌파 (vb) V1 vs V2 backtest 비교 보고서\n")
    lines.append(f"- 생성: {finished_at.isoformat()}")
    lines.append(f"- backtest 기간: 최근 {days}일 (1분봉)")
    lines.append(f"- V1 universe: top {universe_size} (sector blacklist 미적용)")
    lines.append(f"- V2 universe: top {v2_universe_size} (sector blacklist 적용 후)")
    lines.append(f"- 소요 시간: {(finished_at - started_at).total_seconds():.0f}초")
    lines.append("")
    lines.append("## 1. 핵심 요약")
    lines.append("")
    lines.append("| 지표 | V1 (개선 전) | V2 (Tier 1+3+5) | 차이 |")
    lines.append("|---|---:|---:|---:|")
    lines.append(f"| 거래수 | {s1['n']:,} | {s2['n']:,} | {s2['n'] - s1['n']:+,} |")
    lines.append(f"| 승 | {s1['wins']:,} | {s2['wins']:,} | {s2['wins'] - s1['wins']:+,} |")
    lines.append(f"| 패 | {s1['losses']:,} | {s2['losses']:,} | {s2['losses'] - s1['losses']:+,} |")
    lines.append(f"| 승률 | {s1['win_pct']:.2f}% | {s2['win_pct']:.2f}% | {s2['win_pct'] - s1['win_pct']:+.2f}%p |")
    lines.append(f"| 평균 PnL | {_fmt_krw(s1['mean_pnl'])} | {_fmt_krw(s2['mean_pnl'])} | {_fmt_krw(s2['mean_pnl'] - s1['mean_pnl'])} |")
    lines.append(f"| 합산 PnL | {_fmt_krw(s1['total_pnl'])} | {_fmt_krw(s2['total_pnl'])} | {_fmt_krw(s2['total_pnl'] - s1['total_pnl'])} |")
    lines.append(f"| MDD | {_fmt_krw(s1['mdd'])} | {_fmt_krw(s2['mdd'])} | {_fmt_krw(s2['mdd'] - s1['mdd'])} |")
    if s1["total_pnl"]:
        ratio = s2["total_pnl"] / s1["total_pnl"] if s1["total_pnl"] else float("inf")
        lines.append(f"| PnL 배율 (V2/V1) | — | — | **{ratio:.2f}배** |")
    lines.append("")

    # Monthly diff
    lines.append("## 2. 월별 PnL 비교")
    lines.append("")
    lines.append("| 월 | V1 n | V1 승률 | V1 PnL | V2 n | V2 승률 | V2 PnL | PnL 차이 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    all_months = sorted(set(m1.keys()) | set(m2.keys()))
    v1_pos_months = 0
    v2_pos_months = 0
    for mo in all_months:
        a = m1.get(mo, {"n": 0, "pnl": 0, "wins": 0, "losses": 0})
        b = m2.get(mo, {"n": 0, "pnl": 0, "wins": 0, "losses": 0})
        wr1 = (a["wins"] / a["n"] * 100) if a["n"] else 0.0
        wr2 = (b["wins"] / b["n"] * 100) if b["n"] else 0.0
        lines.append(f"| {mo} | {a['n']:,} | {wr1:.1f}% | {_fmt_krw(a['pnl'])} | "
                     f"{b['n']:,} | {wr2:.1f}% | {_fmt_krw(b['pnl'])} | "
                     f"{_fmt_krw(b['pnl'] - a['pnl'])} |")
        if a["pnl"] > 0:
            v1_pos_months += 1
        if b["pnl"] > 0:
            v2_pos_months += 1
    n_months = len(all_months)
    if n_months:
        lines.append(
            f"\n월별 흑자 비율: V1 = {v1_pos_months}/{n_months} "
            f"({v1_pos_months / n_months * 100:.1f}%) / V2 = {v2_pos_months}/{n_months} "
            f"({v2_pos_months / n_months * 100:.1f}%)"
        )
    lines.append("")

    # Symbol-level top diff (V2 PnL - V1 PnL)
    p1 = _per_symbol(v1)
    p2 = _per_symbol(v2)
    all_syms = set(p1.keys()) | set(p2.keys())
    diffs = []
    for s in all_syms:
        a = p1.get(s, {"n": 0, "pnl": 0, "wins": 0})
        b = p2.get(s, {"n": 0, "pnl": 0, "wins": 0})
        diffs.append((s, b["pnl"] - a["pnl"], a, b))
    diffs.sort(key=lambda x: x[1], reverse=True)

    lines.append("## 3. 종목별 V2-V1 PnL 차이")
    lines.append("")
    lines.append("### 3.1 V2 개선 효과 큰 종목 (top 15)")
    lines.append("")
    lines.append("| 종목 | 종목명 | V1 n / PnL | V2 n / PnL | 차이 |")
    lines.append("|---|---|---:|---:|---:|")
    for s, diff, a, b in diffs[:15]:
        name = nm.get(s, "?")
        lines.append(
            f"| {s} | {name} | {a['n']} / {_fmt_krw(a['pnl'])} | "
            f"{b['n']} / {_fmt_krw(b['pnl'])} | **{_fmt_krw(diff)}** |"
        )
    lines.append("")
    lines.append("### 3.2 V2 손해 종목 (bottom 15)")
    lines.append("")
    lines.append("| 종목 | 종목명 | V1 n / PnL | V2 n / PnL | 차이 |")
    lines.append("|---|---|---:|---:|---:|")
    for s, diff, a, b in diffs[-15:]:
        name = nm.get(s, "?")
        lines.append(
            f"| {s} | {name} | {a['n']} / {_fmt_krw(a['pnl'])} | "
            f"{b['n']} / {_fmt_krw(b['pnl'])} | **{_fmt_krw(diff)}** |"
        )
    lines.append("")

    # Top winners / losers per mode
    def _top_per_mode(rows: list[dict], head: str) -> None:
        p = _per_symbol(rows)
        sorted_p = sorted(p.items(), key=lambda kv: kv[1]["pnl"], reverse=True)
        lines.append(f"### {head}")
        lines.append("")
        lines.append("| 종목 | 종목명 | n | PnL |")
        lines.append("|---|---|---:|---:|")
        for sym, st in sorted_p[:10]:
            lines.append(
                f"| {sym} | {nm.get(sym, '?')} | {st['n']} | {_fmt_krw(st['pnl'])} |"
            )
        lines.append("\n…\n")
        lines.append("| 종목 | 종목명 | n | PnL |")
        lines.append("|---|---|---:|---:|")
        for sym, st in sorted_p[-10:]:
            lines.append(
                f"| {sym} | {nm.get(sym, '?')} | {st['n']} | {_fmt_krw(st['pnl'])} |"
            )
        lines.append("")

    lines.append("## 4. 각 모드 종목별 top winner / loser")
    lines.append("")
    _top_per_mode(v1, "4.1 V1 (개선 전)")
    _top_per_mode(v2, "4.2 V2 (Tier 1+3+5)")

    # Conclusion
    lines.append("## 5. 결론 — 어떤 다듬기가 효과 컸나")
    lines.append("")
    delta_pnl = s2["total_pnl"] - s1["total_pnl"]
    delta_wr = s2["win_pct"] - s1["win_pct"]
    delta_n = s2["n"] - s1["n"]
    lines.append(
        f"- 합산 PnL: V1 {_fmt_krw(s1['total_pnl'])} → V2 {_fmt_krw(s2['total_pnl'])} "
        f"({_fmt_krw(delta_pnl)})"
    )
    lines.append(f"- 승률: {delta_wr:+.2f}%p, 거래수: {delta_n:+,}")
    lines.append(f"- MDD: {_fmt_krw(s1['mdd'])} → {_fmt_krw(s2['mdd'])} "
                 f"({_fmt_krw(s2['mdd'] - s1['mdd'])})")
    lines.append("")
    lines.append("주: V2 는 세 Tier 가 결합된 결과 — 개별 기여도는 별도 ablation 필요.")
    lines.append("- Tier 1 (sector blacklist) = trade count 감소 + 승률 상승 효과 (universe 단계)")
    lines.append("- Tier 3 (anchor trailing) = 상승장 양봉 끝까지 잡기")
    lines.append("- Tier 5 (symbol weight) = 음수 평균 종목 차단 + 고승률 종목 ×3 가중")
    lines.append("")
    return "\n".join(lines)


# ---------- Main ----------


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=400,
                   help="lookback days (default 400)")
    p.add_argument("--chunk-size", type=int, default=50,
                   help="universe chunk size (default 50)")
    p.add_argument("--top", type=int, default=0,
                   help="universe top-N (0 = all top_by_market_cap, default)")
    p.add_argument("--out-dir", type=str, default="",
                   help="output dir (default data/reports/vb_compare_YYYYMMDD)")
    p.add_argument("--resume", action="store_true",
                   help="skip chunks whose CSV row already present")
    p.add_argument("--max-chunks", type=int, default=0,
                   help="debug: stop after N chunks (0 = all)")
    p.add_argument("--data-dir", type=str, default="data",
                   help="BarStore root (default 'data', backup 'data/bars/_backups/20260518')")
    p.add_argument("--volume-filter", type=str, default="standard",
                   choices=["off", "conservative", "standard", "aggressive"],
                   help="거래대금 + turnover + RVOL entry gate")
    p.add_argument("--jab-filter", action="store_true",
                   help="동전주/소형시총/SPAC/변동성/유동성 잡주 universe 제외")
    p.add_argument("--entry-gate", action="store_true",
                   help="MTF + KOSPI regime + 시간대 entry confluence filter")
    p.add_argument("--entry-gate-mode", default="abc",
                   choices=["abc", "a_only", "ab", "ac", "bc",
                            "ab_lite", "ac_lite", "ac_soft"],
                   help="ABC 중 활성 component + 완화 옵션")
    p.add_argument("--vb-max-hold", type=int, default=60, help="max_hold_minutes (default 60)")
    p.add_argument("--vb-sl-pct", type=float, default=1.5,
                   help="vb stop_loss_pct (default 1.5%, grid: 1.0/1.5/2.0/2.5/3.0)")
    p.add_argument("--vb-tp-pct", type=float, default=0.0,
                   help="vb take_profit_pct (default 0 = SL × 2 자동. 단타 룰)")
    args = p.parse_args()

    started_at = datetime.now(UTC)
    if not args.out_dir:
        date_tag = started_at.astimezone().strftime("%Y%m%d")
        out_dir = DATA_ROOT / "reports" / f"vb_compare_{date_tag}"
    else:
        out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n=== vb V1 vs V2 backtest | out_dir={out_dir} | days={args.days} "
          f"| chunk_size={args.chunk_size} ===\n")

    bar_store = BarStore(args.data_dir)
    reg = UniverseRegistry("data/universe.sqlite")
    if args.top <= 0:
        universe = reg.top_by_market_cap(100_000)
    else:
        universe = reg.top_by_market_cap(args.top)
    raw_universe_size = len(universe)
    # listed_shares + market_cap — volume filter input
    listed_shares = {e.code: int(getattr(e, "listed_shares", 0) or 0) for e in universe}
    market_cap = {e.code: int(getattr(e, "market_cap_krw", 0) or 0) for e in universe}
    reg.close()

    # daily history (1d) — JAB filter + entry gate MTF 둘 다 사용
    daily_history_global: dict[str, list] = {}
    kospi_history: list = []
    if args.jab_filter or args.entry_gate:
        for e in universe:
            bars_iter = list(bar_store.read(e.code, "1d"))
            if bars_iter:
                daily_history_global[e.code] = bars_iter
        kospi_history = list(bar_store.read("KOSPI", "1d"))
        print(f"  daily history loaded: {len(daily_history_global)} 종목, "
              f"KOSPI {len(kospi_history)} bars")

    # JAB filter — universe pre-filter
    jab_reasons: dict[str, str] = {}
    if args.jab_filter:
        daily_history = {k: v[-60:] for k, v in daily_history_global.items()}
        alert_codes = load_krx_alert_codes(fetch=False)
        jab = JabFilter(daily_history=daily_history, alert_codes=alert_codes)
        passed, jab_reasons = jab.filter_entries(universe)
        # reason 분포
        from collections import Counter
        cnt = Counter(jab_reasons.values())
        print(f"\n=== JAB filter — 제외 {len(jab_reasons)} / {raw_universe_size} ===")
        for reason, c in cnt.most_common(10):
            print(f"  {reason}: {c}")
        universe = passed
        print(f"통과 universe: {len(universe)}")

    all_codes_v1 = [e.code for e in universe]
    print(f"\ndata-dir={args.data_dir}, volume-filter preset={args.volume_filter}, "
          f"jab_filter={args.jab_filter} → universe {len(all_codes_v1)}")

    # V2 codes — sector blacklist filtered
    name_map = _name_map_safe()
    all_codes_v2 = filter_universe(all_codes_v1, name_map)
    print(f"V1 universe = {len(all_codes_v1)} | V2 universe (blacklist 적용) = "
          f"{len(all_codes_v2)} (-{len(all_codes_v1) - len(all_codes_v2)})")

    # Symbol weights (Tier 5)
    sw = SymbolWeightMatrix(db_path=str(DATA_ROOT / "symbol_weights.sqlite"))
    n_loaded = sw.load()
    print(f"SymbolWeightMatrix loaded: {n_loaded} entries")

    # ATR provider — 1d (vb is day_trade style, same as 5/15 backtest)
    atr_other = BarStoreATRProvider(
        bar_store, timeframe="1d", period=14, ttl_seconds=3600,
    )

    # prev_high_low — precompute once for ALL codes (cheap)
    prev_hl_full = compute_prev_high_low(bar_store, all_codes_v1)
    print(f"prev_hl computed for {len(prev_hl_full)} symbols")
    # prev_close — 상한가 즉시 청산 룰의 base price (사용자 룰 5/18)
    prev_close_full = compute_prev_close(bar_store, all_codes_v1)
    print(f"prev_close computed for {len(prev_close_full)} symbols")

    cutoff = datetime.now(UTC) - timedelta(days=args.days)
    print(f"data cutoff: {cutoff.isoformat()}")

    v1_csv = out_dir / "v1_trades.csv"
    v2_csv = out_dir / "v2_trades.csv"
    progress_path = out_dir / "progress.txt"
    processed_offsets: set[int] = set()
    if args.resume and progress_path.exists():
        with open(progress_path) as f:
            for line in f:
                line = line.strip()
                if line.isdigit():
                    processed_offsets.add(int(line))
        print(f"resume: skipping {len(processed_offsets)} processed chunks")

    # Build chunk list — chunks defined by V1 universe ordering. V2 chunk =
    # subset of V1 chunk after blacklist filter.
    n_chunks = (len(all_codes_v1) + args.chunk_size - 1) // args.chunk_size
    print(f"total chunks: {n_chunks}")
    blacklist_set_v2 = set(all_codes_v2)

    chunk_started = time.time()
    last_progress = chunk_started
    total_v1_trades = 0
    total_v2_trades = 0
    total_v3_trades = 0
    total_v4_trades = 0
    for chunk_idx in range(n_chunks):
        offset = chunk_idx * args.chunk_size
        if args.max_chunks and chunk_idx >= args.max_chunks:
            print(f"--max-chunks {args.max_chunks} 도달, 중단")
            break
        if offset in processed_offsets:
            continue
        codes_v1 = all_codes_v1[offset : offset + args.chunk_size]
        codes_v2 = [c for c in codes_v1 if c in blacklist_set_v2]
        chunk_started_at = time.time()

        # Build items — load 1m bars once, share between V1 and V2 (V2 uses
        # subset of symbols but tick filter naturally drops un-strategied
        # symbols downstream). To save memory, we build per-mode item lists
        # only for the codes that mode actually uses.
        prev_hl_v1 = {c: prev_hl_full[c] for c in codes_v1 if c in prev_hl_full}
        prev_hl_v2 = {c: prev_hl_full[c] for c in codes_v2 if c in prev_hl_full}
        prev_close_v1 = {c: prev_close_full[c] for c in codes_v1 if c in prev_close_full}
        prev_close_v2 = {c: prev_close_full[c] for c in codes_v2 if c in prev_close_full}
        # daily_history per chunk — vb 의 entry_ts 기준 직전 일봉 lookup (lookahead fix)
        daily_history_v1 = {c: daily_history_global[c] for c in codes_v1 if c in daily_history_global}
        daily_history_v2 = {c: daily_history_global[c] for c in codes_v2 if c in daily_history_global}

        # Volume filter — mode 별 새 instance (state 격리)
        def _vf():
            return VolumeFilter(
                listed_shares=listed_shares,
                market_cap_krw=market_cap,
                preset=args.volume_filter,
            )

        # Entry gate (MTF + KOSPI regime + 시간대) — mode 별 새 instance
        def _eg():
            if not args.entry_gate:
                return None
            mode = args.entry_gate_mode
            # 완화 mode 처리
            if mode == "ab_lite":  # A + B(완화: MA20 only)
                cfg = EntryGateConfig(
                    enable_time_window=True, enable_market_regime=True, enable_mtf=False,
                    regime_require_ma50=False,
                )
            elif mode == "ac_lite":  # A + C(완화: ATR off)
                cfg = EntryGateConfig(
                    enable_time_window=True, enable_market_regime=False, enable_mtf=True,
                    mtf_require_atr_rising=False,
                )
            elif mode == "ac_soft":  # A + C(더 완화: ATR off + MA20 only)
                cfg = EntryGateConfig(
                    enable_time_window=True, enable_market_regime=False, enable_mtf=True,
                    mtf_require_atr_rising=False, mtf_require_ma50=False,
                )
            else:
                cfg = EntryGateConfig(
                    enable_time_window="a" in mode,
                    enable_market_regime="b" in mode,
                    enable_mtf="c" in mode,
                )
            return EntryGate(
                daily_history=daily_history_global,
                kospi_history=kospi_history,
                config=cfg,
            )

        # V1: build items, run, free before V2 builds. Saves ~50% peak RAM.
        items_v1 = _build_items(bar_store, codes_v1, cutoff)
        items_v1.sort(key=lambda x: x.timestamp)
        fills_v1, n_int_v1 = _run_one(
            mode="v1", codes=codes_v1, bar_store=bar_store, items=items_v1,
            prev_hl=prev_hl_v1, atr_provider=atr_other, symbol_weights=None,
            volume_filter=_vf(), entry_gate=_eg(), stop_loss_pct=args.vb_sl_pct, take_profit_pct=args.vb_tp_pct, max_hold_minutes=args.vb_max_hold,
            prev_close=prev_close_v1, daily_history=daily_history_v1,
        )
        del items_v1
        gc.collect()

        if codes_v2 and prev_hl_v2:
            items_v2 = _build_items(bar_store, codes_v2, cutoff)
            items_v2.sort(key=lambda x: x.timestamp)
            fills_v2, n_int_v2 = _run_one(
                mode="v2", codes=codes_v2, bar_store=bar_store, items=items_v2,
                prev_hl=prev_hl_v2, atr_provider=atr_other, symbol_weights=sw,
                volume_filter=_vf(), entry_gate=_eg(), stop_loss_pct=args.vb_sl_pct, take_profit_pct=args.vb_tp_pct, max_hold_minutes=args.vb_max_hold,
                prev_close=prev_close_v2, daily_history=daily_history_v2,
            )
            fills_v3, n_int_v3 = _run_one(
                mode="v3", codes=codes_v2, bar_store=bar_store, items=items_v2,
                prev_hl=prev_hl_v2, atr_provider=atr_other, symbol_weights=sw,
                volume_filter=_vf(), entry_gate=_eg(), stop_loss_pct=args.vb_sl_pct, take_profit_pct=args.vb_tp_pct, max_hold_minutes=args.vb_max_hold,
                prev_close=prev_close_v2, daily_history=daily_history_v2,
            )
            fills_v4, n_int_v4 = _run_one(
                mode="v4", codes=codes_v2, bar_store=bar_store, items=items_v2,
                prev_hl=prev_hl_v2, atr_provider=atr_other, symbol_weights=None,
                volume_filter=_vf(), entry_gate=_eg(), stop_loss_pct=args.vb_sl_pct, take_profit_pct=args.vb_tp_pct, max_hold_minutes=args.vb_max_hold,
                prev_close=prev_close_v2, daily_history=daily_history_v2,
            )
            del items_v2
            gc.collect()
        else:
            fills_v2, n_int_v2 = [], 0
            fills_v3, n_int_v3 = [], 0
            fills_v4, n_int_v4 = [], 0

        n_v1, n_v2, n_v3, n_v4 = _write_chunk_trades(
            out_dir, fills_v1, fills_v2, fills_v3, fills_v4, offset,
        )
        total_v1_trades += n_v1
        total_v2_trades += n_v2
        total_v3_trades += n_v3
        total_v4_trades += n_v4

        with open(progress_path, "a") as f:
            f.write(f"{offset}\n")

        elapsed = time.time() - chunk_started_at
        cumul = time.time() - chunk_started
        eta_total = cumul / (chunk_idx + 1) * n_chunks
        eta_remain = eta_total - cumul
        print(f"[chunk {chunk_idx + 1}/{n_chunks}] off={offset} "
              f"v1={len(codes_v1)} v2/v4={len(codes_v2)} | "
              f"v1 tr={n_v1} | v2 tr={n_v2} | v3 tr={n_v3} | v4 tr={n_v4} | "
              f"chunk={elapsed:.0f}s eta={eta_remain / 60:.1f}m",
              flush=True)

        # Periodic flush
        if time.time() - last_progress > 60:
            last_progress = time.time()

    finished_at = datetime.now(UTC)
    v3_csv = out_dir / "v3_trades.csv"
    v4_csv = out_dir / "v4_trades.csv"
    print(f"\n=== Aggregating | v1={total_v1_trades} v2={total_v2_trades} "
          f"v3={total_v3_trades} v4={total_v4_trades} ===")

    v1_rows = _read_trades(v1_csv)
    v2_rows = _read_trades(v2_csv)
    v3_rows = _read_trades(v3_csv)
    v4_rows = _read_trades(v4_csv)
    print(f"loaded from CSV: v1={len(v1_rows)} v2={len(v2_rows)} "
          f"v3={len(v3_rows)} v4={len(v4_rows)}")

    # V1 vs V2 (기존 보고서 — 호환 유지)
    report_v1v2 = _build_report(
        v1_rows, v2_rows,
        universe_size=len(all_codes_v1),
        v2_universe_size=len(all_codes_v2),
        days=args.days,
        started_at=started_at,
        finished_at=finished_at,
    )
    (out_dir / "report.md").write_text(report_v1v2, encoding="utf-8")

    # V1/V2/V3 3-way (호환)
    report_3way = _build_report_3way(
        v1_rows, v2_rows, v3_rows,
        universe_size=len(all_codes_v1),
        v2_universe_size=len(all_codes_v2),
        days=args.days,
        started_at=started_at,
        finished_at=finished_at,
    )
    (out_dir / "report_3way.md").write_text(report_3way, encoding="utf-8")

    # V1/V2/V3/V4 4-way 보고서
    report_4way = _build_report_4way(
        v1_rows, v2_rows, v3_rows, v4_rows,
        universe_size=len(all_codes_v1),
        v2_universe_size=len(all_codes_v2),
        days=args.days,
        started_at=started_at,
        finished_at=finished_at,
    )
    report4_path = out_dir / "report_4way.md"
    report4_path.write_text(report_4way, encoding="utf-8")

    print(f"\n[report 4-way] → {report4_path}")
    print(f"[csv v1/v2/v3/v4] → {out_dir}/v*_trades.csv")
    print("\n----- 4-way 보고서 (stdout) -----\n")
    print(report_4way)
    return 0


def _build_report_4way(
    v1: list[dict], v2: list[dict], v3: list[dict], v4: list[dict], *,
    universe_size: int, v2_universe_size: int,
    days: int, started_at: datetime, finished_at: datetime,
) -> str:
    """V1/V2/V3/V4 통합 비교 — V4 = vb + sector blacklist 만 (fixed, no
    symbol_weight, no trailing) 의 honest 효과 검증."""
    nm = _name_map_safe()

    def _fmt(n: int) -> str:
        return f"{n:+,}"

    def _summary(rows: list[dict]) -> dict:
        if not rows:
            return {"n": 0, "wins": 0, "losses": 0, "win_pct": 0.0,
                    "mean_pnl": 0, "total_pnl": 0, "mdd": 0, "avg_hold": 0.0}
        wins = sum(1 for r in rows if r["pnl_krw"] > 0)
        losses = sum(1 for r in rows if r["pnl_krw"] < 0)
        mdd, _ = _mdd(rows)
        return {
            "n": len(rows), "wins": wins, "losses": losses,
            "win_pct": wins / len(rows) * 100,
            "mean_pnl": int(statistics.mean(r["pnl_krw"] for r in rows)),
            "total_pnl": sum(r["pnl_krw"] for r in rows),
            "mdd": mdd,
            "avg_hold": statistics.mean(r["hold_minutes"] for r in rows),
        }

    s1, s2, s3, s4 = _summary(v1), _summary(v2), _summary(v3), _summary(v4)
    m1, m2, m3, m4 = _monthly_pnl(v1), _monthly_pnl(v2), _monthly_pnl(v3), _monthly_pnl(v4)

    lines: list[str] = []
    lines.append("# 변동성돌파 V1 / V2 / V3 / V4 4-way backtest 보고서\n")
    lines.append(f"- 생성: {finished_at.isoformat()}")
    lines.append(f"- 기간: 최근 {days}일 (1분봉)")
    lines.append(f"- V1 universe: top {universe_size} (blacklist 미적용)")
    lines.append(f"- V2/V3/V4 universe: top {v2_universe_size} (blacklist 적용)")
    lines.append(f"- 소요 시간: {(finished_at - started_at).total_seconds():.0f}초")
    lines.append("")
    lines.append("## 모드 정의")
    lines.append("- **V1** = vb baseline (전체 universe, 다듬기 없음)")
    lines.append("- **V2** = vb + Tier 1 (blacklist) + Tier 3 (trailing) + Tier 5 (symbol_weight)")
    lines.append("- **V3** = vb_scalein + V2 의 universe/weight (분할매수/매도)")
    lines.append("- **V4** = vb + Tier 1 (blacklist) 만 — fixed rule, no symbol_weight, no trailing")
    lines.append("")
    lines.append("## 1. 핵심 요약")
    lines.append("")
    lines.append("| 지표 | V1 | V2 | V3 | V4 | V4-V1 | V4-V2 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    lines.append(f"| 거래수 | {s1['n']:,} | {s2['n']:,} | {s3['n']:,} | {s4['n']:,} | {s4['n']-s1['n']:+,} | {s4['n']-s2['n']:+,} |")
    lines.append(f"| 승률 | {s1['win_pct']:.2f}% | {s2['win_pct']:.2f}% | {s3['win_pct']:.2f}% | {s4['win_pct']:.2f}% | {s4['win_pct']-s1['win_pct']:+.2f}%p | {s4['win_pct']-s2['win_pct']:+.2f}%p |")
    lines.append(f"| 평균 PnL | {_fmt(s1['mean_pnl'])} | {_fmt(s2['mean_pnl'])} | {_fmt(s3['mean_pnl'])} | {_fmt(s4['mean_pnl'])} | {_fmt(s4['mean_pnl']-s1['mean_pnl'])} | {_fmt(s4['mean_pnl']-s2['mean_pnl'])} |")
    lines.append(f"| 합산 PnL | {_fmt(s1['total_pnl'])} | {_fmt(s2['total_pnl'])} | {_fmt(s3['total_pnl'])} | {_fmt(s4['total_pnl'])} | {_fmt(s4['total_pnl']-s1['total_pnl'])} | {_fmt(s4['total_pnl']-s2['total_pnl'])} |")
    lines.append(f"| MDD | {_fmt(s1['mdd'])} | {_fmt(s2['mdd'])} | {_fmt(s3['mdd'])} | {_fmt(s4['mdd'])} | — | — |")
    lines.append(f"| 평균 보유 (분) | {s1['avg_hold']:.1f} | {s2['avg_hold']:.1f} | {s3['avg_hold']:.1f} | {s4['avg_hold']:.1f} | — | — |")
    lines.append("")

    lines.append("## 2. 월별 PnL 비교")
    lines.append("")
    lines.append("| 월 | V1 PnL | V2 PnL | V3 PnL | V4 PnL |")
    lines.append("|---|---:|---:|---:|---:|")
    all_months = sorted(set(m1.keys()) | set(m2.keys()) | set(m3.keys()) | set(m4.keys()))
    v4_pos_months = 0
    for mo in all_months:
        a = m1.get(mo, {"n": 0, "pnl": 0})
        b = m2.get(mo, {"n": 0, "pnl": 0})
        c = m3.get(mo, {"n": 0, "pnl": 0})
        d = m4.get(mo, {"n": 0, "pnl": 0})
        lines.append(
            f"| {mo} | {_fmt(a['pnl'])} | {_fmt(b['pnl'])} | "
            f"{_fmt(c['pnl'])} | {_fmt(d['pnl'])} |"
        )
        if d["pnl"] > 0:
            v4_pos_months += 1
    n_months = len(all_months)
    if n_months:
        lines.append(
            f"\n월별 흑자 비율: V4 = {v4_pos_months}/{n_months} "
            f"({v4_pos_months / n_months * 100:.1f}%)"
        )
    lines.append("")

    lines.append("## 3. 결론 — Tier 별 분리 효과")
    lines.append("")
    lines.append(f"- V1 → V4 (Tier 1 blacklist 만): PnL {_fmt(s1['total_pnl'])} → {_fmt(s4['total_pnl'])} ({_fmt(s4['total_pnl']-s1['total_pnl'])}), 승률 {s4['win_pct']-s1['win_pct']:+.2f}%p")
    lines.append(f"- V4 → V2 (+ Tier 3 trailing + Tier 5 symbol_weight): PnL {_fmt(s4['total_pnl'])} → {_fmt(s2['total_pnl'])} ({_fmt(s2['total_pnl']-s4['total_pnl'])}), 승률 {s2['win_pct']-s4['win_pct']:+.2f}%p")
    lines.append("")
    if s4["total_pnl"] > s1["total_pnl"]:
        lines.append("→ ✅ sector blacklist (고정 룰) 만으로도 V1 baseline 개선 확인")
    else:
        lines.append("→ ❌ sector blacklist 만으로는 V1 개선 X — blacklist 도 효과 의심")
    if s2["total_pnl"] > s4["total_pnl"]:
        lines.append("→ ✅ Tier 3/5 가 추가 가치 — V4 위에서 더 좋음 (단 Tier 5 overfit 의심 여전)")
    else:
        lines.append("→ ⚠️ Tier 3/5 (특히 Tier 5 symbol_weight) 가 V4 보다 좋지 X — overfit 의심 검증됨")
    lines.append("")
    return "\n".join(lines)


def _build_report_3way(
    v1: list[dict], v2: list[dict], v3: list[dict], *,
    universe_size: int, v2_universe_size: int,
    days: int, started_at: datetime, finished_at: datetime,
) -> str:
    """V1 / V2 / V3 통합 3-way 비교 보고서. V2 baseline 으로 V3 (vb_scalein)
    의 분할매수/매도 효과 평가에 초점."""
    nm = _name_map_safe()

    def _fmt(n: int) -> str:
        return f"{n:+,}"

    def _summary(rows: list[dict]) -> dict:
        if not rows:
            return {"n": 0, "wins": 0, "losses": 0, "win_pct": 0.0,
                    "mean_pnl": 0, "total_pnl": 0, "mdd": 0,
                    "avg_hold": 0.0}
        wins = sum(1 for r in rows if r["pnl_krw"] > 0)
        losses = sum(1 for r in rows if r["pnl_krw"] < 0)
        mdd, _ = _mdd(rows)
        return {
            "n": len(rows),
            "wins": wins,
            "losses": losses,
            "win_pct": wins / len(rows) * 100,
            "mean_pnl": int(statistics.mean(r["pnl_krw"] for r in rows)),
            "total_pnl": sum(r["pnl_krw"] for r in rows),
            "mdd": mdd,
            "avg_hold": statistics.mean(r["hold_minutes"] for r in rows),
        }

    s1 = _summary(v1)
    s2 = _summary(v2)
    s3 = _summary(v3)
    m1 = _monthly_pnl(v1)
    m2 = _monthly_pnl(v2)
    m3 = _monthly_pnl(v3)

    lines: list[str] = []
    lines.append("# 변동성돌파 V1 / V2 / V3 3-way backtest 보고서\n")
    lines.append(f"- 생성: {finished_at.isoformat()}")
    lines.append(f"- 기간: 최근 {days}일 (1분봉)")
    lines.append(f"- V1 universe: top {universe_size} (sector blacklist 미적용)")
    lines.append(f"- V2/V3 universe: top {v2_universe_size} (blacklist 적용)")
    lines.append(f"- 소요 시간: {(finished_at - started_at).total_seconds():.0f}초")
    lines.append("")
    lines.append("## 모드 정의")
    lines.append("- **V1** (개선 전): vb baseline, trailing/blacklist/symbol_weight 없음")
    lines.append("- **V2** (vb tuned): vb + Tier 1 blacklist + Tier 3 anchor trailing + Tier 5 symbol_weight")
    lines.append("- **V3** (vb_scalein): V2 의 universe/weight 그대로 + 분할매수 (50/30/20) + 분할매도 (33/33/잔여 trailing)")
    lines.append("")
    lines.append("## 1. 핵심 요약")
    lines.append("")
    lines.append("| 지표 | V1 | V2 | V3 | V3-V2 |")
    lines.append("|---|---:|---:|---:|---:|")
    lines.append(f"| 거래수 | {s1['n']:,} | {s2['n']:,} | {s3['n']:,} | {s3['n'] - s2['n']:+,} |")
    lines.append(f"| 승률 | {s1['win_pct']:.2f}% | {s2['win_pct']:.2f}% | {s3['win_pct']:.2f}% | {s3['win_pct'] - s2['win_pct']:+.2f}%p |")
    lines.append(f"| 평균 PnL | {_fmt(s1['mean_pnl'])} | {_fmt(s2['mean_pnl'])} | {_fmt(s3['mean_pnl'])} | {_fmt(s3['mean_pnl'] - s2['mean_pnl'])} |")
    lines.append(f"| 합산 PnL | {_fmt(s1['total_pnl'])} | {_fmt(s2['total_pnl'])} | {_fmt(s3['total_pnl'])} | {_fmt(s3['total_pnl'] - s2['total_pnl'])} |")
    lines.append(f"| MDD | {_fmt(s1['mdd'])} | {_fmt(s2['mdd'])} | {_fmt(s3['mdd'])} | {_fmt(s3['mdd'] - s2['mdd'])} |")
    lines.append(f"| 평균 보유 (분) | {s1['avg_hold']:.1f} | {s2['avg_hold']:.1f} | {s3['avg_hold']:.1f} | {s3['avg_hold'] - s2['avg_hold']:+.1f} |")
    lines.append("")

    lines.append("## 2. 월별 PnL 비교")
    lines.append("")
    lines.append("| 월 | V1 n / PnL | V2 n / PnL | V3 n / PnL | V3-V2 |")
    lines.append("|---|---:|---:|---:|---:|")
    all_months = sorted(set(m1.keys()) | set(m2.keys()) | set(m3.keys()))
    v3_pos_months = 0
    for mo in all_months:
        a = m1.get(mo, {"n": 0, "pnl": 0})
        b = m2.get(mo, {"n": 0, "pnl": 0})
        c = m3.get(mo, {"n": 0, "pnl": 0})
        lines.append(
            f"| {mo} | {a['n']:,} / {_fmt(a['pnl'])} | "
            f"{b['n']:,} / {_fmt(b['pnl'])} | "
            f"{c['n']:,} / {_fmt(c['pnl'])} | "
            f"{_fmt(c['pnl'] - b['pnl'])} |"
        )
        if c["pnl"] > 0:
            v3_pos_months += 1
    n_months = len(all_months)
    if n_months:
        lines.append(
            f"\n월별 흑자 비율: V3 = {v3_pos_months}/{n_months} "
            f"({v3_pos_months / n_months * 100:.1f}%)"
        )
    lines.append("")

    # V2 vs V3 종목별 diff
    p2 = _per_symbol(v2)
    p3 = _per_symbol(v3)
    all_syms = set(p2.keys()) | set(p3.keys())
    diffs = []
    for s in all_syms:
        b = p2.get(s, {"n": 0, "pnl": 0, "wins": 0})
        c = p3.get(s, {"n": 0, "pnl": 0, "wins": 0})
        diffs.append((s, c["pnl"] - b["pnl"], b, c))
    diffs.sort(key=lambda x: x[1], reverse=True)

    lines.append("## 3. 종목별 V3-V2 PnL 차이")
    lines.append("")
    lines.append("### 3.1 V3 개선 종목 (top 15)")
    lines.append("")
    lines.append("| 종목 | 종목명 | V2 n / PnL | V3 n / PnL | 차이 |")
    lines.append("|---|---|---:|---:|---:|")
    for s, diff, b, c in diffs[:15]:
        name = nm.get(s, "?")
        lines.append(
            f"| {s} | {name} | {b['n']} / {_fmt(b['pnl'])} | "
            f"{c['n']} / {_fmt(c['pnl'])} | **{_fmt(diff)}** |"
        )
    lines.append("")
    lines.append("### 3.2 V3 손해 종목 (bottom 15)")
    lines.append("")
    lines.append("| 종목 | 종목명 | V2 n / PnL | V3 n / PnL | 차이 |")
    lines.append("|---|---|---:|---:|---:|")
    for s, diff, b, c in diffs[-15:]:
        name = nm.get(s, "?")
        lines.append(
            f"| {s} | {name} | {b['n']} / {_fmt(b['pnl'])} | "
            f"{c['n']} / {_fmt(c['pnl'])} | **{_fmt(diff)}** |"
        )
    lines.append("")

    lines.append("## 4. 결론 — 분할매수/매도가 vb 대비 효과 있나")
    lines.append("")
    delta_pnl = s3["total_pnl"] - s2["total_pnl"]
    delta_wr = s3["win_pct"] - s2["win_pct"]
    delta_mdd = s3["mdd"] - s2["mdd"]
    lines.append(f"- PnL: V2 {_fmt(s2['total_pnl'])} → V3 {_fmt(s3['total_pnl'])} ({_fmt(delta_pnl)})")
    lines.append(f"- 승률: {delta_wr:+.2f}%p")
    lines.append(f"- MDD: {_fmt(s2['mdd'])} → {_fmt(s3['mdd'])} ({_fmt(delta_mdd)})")
    lines.append(f"- 평균 보유: {s2['avg_hold']:.1f}분 → {s3['avg_hold']:.1f}분")
    lines.append("")
    if delta_pnl > 0 and delta_wr > 0:
        lines.append("→ ✅ V3 분할매매 = vb tuned 보다 우위 (PnL + 승률 모두 개선)")
    elif delta_pnl > 0:
        lines.append("→ ⚠️ V3 PnL 개선했으나 승률 X — 작은 이익 누적 효과 (긍정 신호)")
    elif delta_wr > 0:
        lines.append("→ ⚠️ V3 승률 개선했으나 PnL X — TP 단계 너무 짧을 가능성 (재조정 필요)")
    else:
        lines.append("→ ❌ V3 가 vb tuned 보다 못함 — 분할 단계/비율 재조정 또는 polling 룰 추가 필요")
    lines.append("")
    lines.append("주의: V3 의 거래수는 부분 청산이 별도 row 로 카운트되므로 V1/V2 와 단순 비교 X.")
    lines.append("승률은 부분 청산 기준 win/loss 라 명목 승률이 V2 보다 높게 나올 수 있음.")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
