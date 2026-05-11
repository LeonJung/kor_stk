"""Historical emulation runner — 각 strategy 별 universe + 시간대 룰 적용,
2주치 일봉 데이터로 백테스트, 중간 결과 + +수익 종목 list 저장.

Phase:
1. UniverseRegistry 에서 strategy 별 universe 결정 (시총/섹터/우선주 등)
2. BarStore 에서 2주치 일봉 read
3. 각 strategy 별 BacktestDriver 또는 TickReplayDriver 실행
   - bar 기반 strategy: BacktestDriver
   - event 기반 strategy: 합성 events (LimitUpReached 등) 시뮬레이션
4. WinningTradesRegistry 에 +수익 종목 기록
5. strategy 별 중간/최종 결과 print

NOTE: tick-level strategy (PairFollow, OpeningMomentum, VWAP) 는 일봉만으론
정확 백테스트 불가. 일봉 high/low 로 OHLC entry 가정 + 단순 trigger.
정밀 검증은 분봉/틱 데이터 (Phase 2/3) 후속.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ks_ws.backtest.driver import BacktestDriver, BacktestResult
from ks_ws.domain import Bar
from ks_ws.storage.bars import BarStore
from ks_ws.storage.universe import UniverseEntry, UniverseRegistry


# -------- Winning trades registry ----------------------------------------

_WIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS winning_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    open_date TEXT NOT NULL,
    close_date TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    entry_price INTEGER NOT NULL,
    exit_price INTEGER NOT NULL,
    pnl_krw INTEGER NOT NULL,
    pnl_pct REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_win_strategy ON winning_trades(strategy);
CREATE INDEX IF NOT EXISTS idx_win_symbol ON winning_trades(symbol);
"""


class WinningTradesRegistry:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.executescript(_WIN_SCHEMA)
        self._conn.commit()

    def record(self, strategy: str, symbol: str, open_date: date, close_date: date,
               quantity: int, entry_price: int, exit_price: int) -> None:
        # 사용자 요청 2026-05-10: wins + losses 모두 저장 (net PnL 계산용)
        if entry_price <= 0:
            return
        pnl = (exit_price - entry_price) * quantity
        pnl_pct = (exit_price - entry_price) / entry_price * 100
        self._conn.execute(
            """
            INSERT INTO winning_trades
            (strategy, symbol, open_date, close_date, quantity, entry_price, exit_price, pnl_krw, pnl_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (strategy, symbol, open_date.isoformat(), close_date.isoformat(),
             quantity, entry_price, exit_price, pnl, pnl_pct),
        )
        self._conn.commit()

    def summary(self) -> dict:
        cur = self._conn.execute(
            """
            SELECT strategy,
                   COUNT(*)                                      AS trades,
                   SUM(CASE WHEN pnl_krw > 0 THEN 1 ELSE 0 END)  AS wins,
                   SUM(CASE WHEN pnl_krw < 0 THEN 1 ELSE 0 END)  AS losses,
                   SUM(CASE WHEN pnl_krw > 0 THEN pnl_krw ELSE 0 END) AS win_krw,
                   SUM(CASE WHEN pnl_krw < 0 THEN pnl_krw ELSE 0 END) AS loss_krw,
                   SUM(pnl_krw)                                  AS net_krw,
                   AVG(pnl_pct)                                  AS avg_pct,
                   MAX(pnl_pct)                                  AS max_pct,
                   MIN(pnl_pct)                                  AS min_pct
            FROM winning_trades GROUP BY strategy
            """
        )
        out = {}
        for row in cur.fetchall():
            (strat, trades, wins, losses, win_krw, loss_krw, net_krw,
             avg_pct, max_pct, min_pct) = row
            out[strat] = dict(
                trades=trades, wins=wins, losses=losses,
                win_krw=win_krw or 0, loss_krw=loss_krw or 0, net_krw=net_krw or 0,
                avg_pct=avg_pct or 0, max_pct=max_pct or 0, min_pct=min_pct or 0,
                win_rate=wins / trades if trades else 0,
            )
        return out

    def close(self) -> None:
        self._conn.close()


# -------- Per-strategy universe selection --------------------------------


# Strategy 별 데이터 사양 — 어떤 데이터 (universe + timeframe + 시간대 + lookback)
# 를 보는지 표시용. (사용자 요청 2026-05-10)
STRATEGY_SPECS = {
    "closing_bet":         dict(uni="KOSPI+KOSDAQ 보통주 300",   tf="1d",      window="EOD entry → 다음날 OHLC exit"),
    "bottom_volume_spike": dict(uni="KOSDAQ 시총상위 300",       tf="1d",      window="60일 lookback + 거래량 spike"),
    "breakout":            dict(uni="시총상위 100",               tf="1d",      window="60일 신고가 + 거래량 ↑"),
    "microcap":            dict(uni="KOSDAQ 시총 1천억↓ 200",     tf="1d",      window="5일 vol_avg + 다음날"),
    "preferred_pair":      dict(uni="우선주 90종목 (1d 종가 ratio)", tf="1d",   window="warmup 30일 + ±2σ entry"),
    "crash_recovery":      dict(uni="KOSPI 시총상위 50",          tf="1d",      window="KOSPI -2% 다음날 시초가"),
    "opening_momentum_min":dict(uni="시총상위 100",               tf="1m",      window="09:00 open + 09:03~09:25 entry / 09:50 force"),
    "vwap_reversion_min":  dict(uni="KOSPI 시총상위 200",         tf="1m",      window="장중 VWAP rolling 30 + ±1.5σ + vol×3"),
    "scalping_min":        dict(uni="KOSDAQ 시총상위 300",        tf="1m",      window="09:00~09:50 +0.5% spike + vol ×3"),
    "pair_follow_min":     dict(uni="KOSDAQ 시총상위 200 페어",   tf="1m+1d",   window="leader 분봉 high ≥ prev×1.30"),
}


def choose_universe_for_strategy(strategy: str, reg: UniverseRegistry) -> list[UniverseEntry]:
    """Return universe entries for a given strategy based on its rules."""
    if strategy == "pair_follow":
        # 시총 상위 200 (테마 페어 후보로 충분)
        return reg.top_by_market_cap(200)
    if strategy == "opening_momentum":
        # 거래대금 활발 후보 — 시총 상위 100
        return reg.top_by_market_cap(100)
    if strategy == "closing_bet":
        # 모든 보통주 (도지 패턴은 어디든 가능)
        return reg.all(markets=("KOSPI", "KOSDAQ"), exclude_preferred=True, exclude_spac=True)[:300]
    if strategy == "vwap_reversion":
        # KOSPI200 대형주 위주 (호재 종목 NLP 필터는 미구현 → 전체)
        return reg.top_by_market_cap(200, market="KOSPI")
    if strategy == "preferred_pair":
        # 우선주 활성 종목들
        return [e for e in reg.all() if e.is_preferred]
    if strategy == "inst_fgn_flow":
        return reg.top_by_market_cap(100)
    if strategy == "bottom_volume_spike":
        # 코스닥 위주
        return reg.top_by_market_cap(300, market="KOSDAQ")
    if strategy == "microcap":
        # 코스닥 시총 1천억 이하
        return [
            e for e in reg.all(markets=("KOSDAQ",), exclude_preferred=True, exclude_spac=True)
            if e.market_cap_krw is not None and e.market_cap_krw <= 100_000_000_000
        ][:200]
    if strategy == "crash_recovery":
        return reg.top_by_market_cap(50, market="KOSPI")
    if strategy == "large_cap_basket":
        return reg.top_by_market_cap(50, market="KOSPI")
    if strategy == "opening_momentum_min":
        return reg.top_by_market_cap(100)
    if strategy == "vwap_reversion_min":
        return reg.top_by_market_cap(200, market="KOSPI")
    if strategy == "scalping_min":
        return reg.top_by_market_cap(300, market="KOSDAQ")
    if strategy == "pair_follow_min":
        return reg.top_by_market_cap(200, market="KOSDAQ")
    return reg.top_by_market_cap(100)


# -------- Bar-based simple simulators ------------------------------------


def simulate_doji_closing_bet(bars_by_symbol: dict[str, list[Bar]], win_reg: WinningTradesRegistry,
                               body_pct_threshold: float = 0.3, take_profit_pct: float = 2.0,
                               stop_loss_pct: float = 3.0) -> dict:
    """일봉 도지 → 다음날 시초가 entry, 다음날 종가/저점/고점 기준 청산.
    실 strategy 의 V1 단순 모델."""
    stats = dict(trades=0, wins=0, losses=0, total_pnl=0.0)
    for symbol, bars in bars_by_symbol.items():
        for i in range(len(bars) - 1):
            b = bars[i]
            nb = bars[i + 1]
            if b.open <= 0:
                continue
            body_pct = abs(b.open - b.close) / b.open * 100
            range_pct = (b.high - b.low) / b.open * 100
            if body_pct >= body_pct_threshold or range_pct < 0.5:
                continue
            entry = nb.open
            tp = entry * (1 + take_profit_pct / 100)
            sl = entry * (1 - stop_loss_pct / 100)
            # 다음날 high/low 로 hit 판단
            if nb.low <= sl:
                exit_price = int(sl)  # 보수적: 일봉 high+low 동시 hit 시 손절 먼저 가정
            elif nb.high >= tp:
                exit_price = int(tp)
            else:
                exit_price = nb.close
            qty = 10
            pnl = (exit_price - entry) * qty
            stats["trades"] += 1
            stats["total_pnl"] += pnl
            win_reg.record("closing_bet", symbol, b.timestamp.date(), nb.timestamp.date(),
                               qty, entry, exit_price)
            if pnl > 0:
                stats["wins"] += 1
            elif pnl < 0:
                stats["losses"] += 1
    return stats


def simulate_60day_low_recovery(bars_by_symbol: dict[str, list[Bar]], win_reg: WinningTradesRegistry,
                                 take_profit_pct: float = 7.0,
                                 stop_below_low_pct: float = 2.0) -> dict:
    """60일 저점 ±5% + 직전 5일 거래량 평균 ×2 이상 → BUY (다음날 시초가),
    이후 +N% 익절 / 60일 저점 -2% 손절."""
    stats = dict(trades=0, wins=0, losses=0, total_pnl=0.0)
    for symbol, bars in bars_by_symbol.items():
        if len(bars) < 11:
            continue
        for i in range(10, len(bars) - 1):
            window = bars[max(0, i - 60):i + 1]
            if len(window) < 11:
                continue
            low = min(b.close for b in window)
            cur = bars[i]
            upper_band = low * 1.05
            if cur.close > upper_band:
                continue
            recent_vol = [b.volume for b in window[-5:]]
            prior_vol = [b.volume for b in window[:-5]]
            if not prior_vol or sum(prior_vol) == 0:
                continue
            recent_avg = sum(recent_vol) / len(recent_vol)
            prior_avg = sum(prior_vol) / len(prior_vol)
            if recent_avg < prior_avg * 2.0:
                continue
            nb = bars[i + 1]
            entry = nb.open
            tp = entry * (1 + take_profit_pct / 100)
            sl = low * (1 - stop_below_low_pct / 100)
            if nb.low <= sl:
                exit_price = int(sl)  # 보수적: 일봉 high+low 동시 hit 시 손절 먼저 가정
            elif nb.high >= tp:
                exit_price = int(tp)
            else:
                exit_price = nb.close
            qty = 10
            pnl = (exit_price - entry) * qty
            stats["trades"] += 1
            stats["total_pnl"] += pnl
            win_reg.record("bottom_volume_spike", symbol, cur.timestamp.date(),
                               nb.timestamp.date(), qty, entry, exit_price)
            if pnl > 0:
                stats["wins"] += 1
            elif pnl < 0:
                stats["losses"] += 1
            break  # one entry per window for V1
    return stats


def simulate_breakout(bars_by_symbol: dict[str, list[Bar]], win_reg: WinningTradesRegistry,
                       lookback_days: int = 60, take_profit_pct: float = 2.0,
                       stop_loss_pct: float = 3.0) -> dict:
    """C 돌파: 종가가 60일 max close 돌파 + 거래량 ↑ → 다음날 시초가 entry,
    +2% 익절 / -3% 손절."""
    stats = dict(trades=0, wins=0, losses=0, total_pnl=0.0)
    for symbol, bars in bars_by_symbol.items():
        if len(bars) < lookback_days + 1:
            continue
        for i in range(lookback_days, len(bars) - 1):
            window = bars[i - lookback_days : i]
            high60 = max(b.close for b in window)
            cur = bars[i]
            if cur.close <= high60:
                continue
            # volume confirmation: 직전 5일 평균보다 1.5×
            recent_vol = [b.volume for b in window[-5:]]
            avg_vol = sum(recent_vol) / len(recent_vol) if recent_vol else 0
            if avg_vol == 0 or cur.volume < avg_vol * 1.5:
                continue
            nb = bars[i + 1]
            entry = nb.open
            tp = entry * (1 + take_profit_pct / 100)
            sl = entry * (1 - stop_loss_pct / 100)
            if nb.low <= sl:
                exit_price = int(sl)  # 보수적: 일봉 high+low 동시 hit 시 손절 먼저 가정
            elif nb.high >= tp:
                exit_price = int(tp)
            else:
                exit_price = nb.close
            qty = 10
            pnl = (exit_price - entry) * qty
            stats["trades"] += 1
            stats["total_pnl"] += pnl
            win_reg.record("breakout", symbol, cur.timestamp.date(),
                               nb.timestamp.date(), qty, entry, exit_price)
            if pnl > 0:
                stats["wins"] += 1
            elif pnl < 0:
                stats["losses"] += 1
    return stats


def simulate_microcap(bars_by_symbol: dict[str, list[Bar]], win_reg: WinningTradesRegistry,
                      take_profit_pct: float = 5.0, stop_loss_pct: float = 1.5) -> dict:
    """Sec 14 MicroCap: 일봉 거래량 spike (전일 ×3) → 다음날 시초가 entry,
    +5% 익절 / -1.5% 엄격 손절."""
    stats = dict(trades=0, wins=0, losses=0, total_pnl=0.0)
    for symbol, bars in bars_by_symbol.items():
        if len(bars) < 6:
            continue
        for i in range(5, len(bars) - 1):
            avg_vol = sum(b.volume for b in bars[i - 5 : i]) / 5
            if avg_vol == 0:
                continue
            cur = bars[i]
            if cur.volume < avg_vol * 3.0:
                continue
            # 음봉이면 skip (호재 spike 만)
            if cur.close <= cur.open:
                continue
            nb = bars[i + 1]
            entry = nb.open
            tp = entry * (1 + take_profit_pct / 100)
            sl = entry * (1 - stop_loss_pct / 100)
            if nb.low <= sl:
                exit_price = int(sl)  # 보수적: 일봉 high+low 동시 hit 시 손절 먼저 가정
            elif nb.high >= tp:
                exit_price = int(tp)
            else:
                exit_price = nb.close
            qty = 10
            pnl = (exit_price - entry) * qty
            stats["trades"] += 1
            stats["total_pnl"] += pnl
            win_reg.record("microcap", symbol, cur.timestamp.date(),
                               nb.timestamp.date(), qty, entry, exit_price)
            if pnl > 0:
                stats["wins"] += 1
            elif pnl < 0:
                stats["losses"] += 1
    return stats


def simulate_preferred_pair(reg, bar_store, win_reg: WinningTradesRegistry,
                             start, end, entry_sigma: float = 2.0,
                             warmup: int = 30) -> dict:
    """H PreferredCommonPair: 우선주↔본주 일봉 종가 ratio 의 rolling mean ± σ
    이탈 → 양방향 진입. 평균 회귀 시 청산."""
    import statistics
    from collections import deque

    stats = dict(trades=0, wins=0, losses=0, total_pnl=0.0)
    universe = reg.all()
    by_code = {e.code: e for e in universe}
    pref_map = {}  # pref → common
    for e in universe:
        if e.is_preferred and e.code[-1] in "5679":
            common = e.code[:-1] + "0"
            if common in by_code:
                pref_map[e.code] = common

    for pref, common in pref_map.items():
        pref_bars = list(bar_store.read(pref, "1d", start=start, end=end))
        common_bars = list(bar_store.read(common, "1d", start=start, end=end))
        if len(pref_bars) < warmup + 5 or len(common_bars) < warmup + 5:
            continue
        pref_by_d = {b.timestamp.date(): b for b in pref_bars}
        common_by_d = {b.timestamp.date(): b for b in common_bars}
        common_dates = sorted(set(pref_by_d) & set(common_by_d))
        if len(common_dates) < warmup + 5:
            continue
        ratios: deque = deque(maxlen=warmup * 2)
        for d in common_dates[:warmup]:
            ratios.append(pref_by_d[d].close / common_by_d[d].close)
        for i in range(warmup, len(common_dates) - 1):
            d = common_dates[i]
            r = pref_by_d[d].close / common_by_d[d].close
            ratios.append(r)
            if len(ratios) < warmup:
                continue
            mean = statistics.fmean(ratios)
            sigma = statistics.pstdev(ratios)
            if sigma <= 0:
                continue
            dev = (r - mean) / sigma
            if abs(dev) < entry_sigma:
                continue
            # entry next day at open; exit when ratio reverts
            for j in range(i + 1, len(common_dates) - 1):
                nd = common_dates[j]
                r_next = pref_by_d[nd].close / common_by_d[nd].close
                dev_next = (r_next - mean) / sigma
                if (dev > 0 and dev_next <= 0) or (dev < 0 and dev_next >= 0):
                    # closed via mean revert. PnL = ratio movement * qty (단순화)
                    qty = 10
                    if dev > 0:  # short pref, long common
                        pnl_per_share = (
                            (pref_by_d[d].close - pref_by_d[nd].close)  # pref short profit
                            + (common_by_d[nd].close - common_by_d[d].close)
                        )
                    else:
                        pnl_per_share = (
                            (pref_by_d[nd].close - pref_by_d[d].close)
                            + (common_by_d[d].close - common_by_d[nd].close)
                        )
                    pnl = pnl_per_share * qty
                    stats["trades"] += 1
                    stats["total_pnl"] += pnl
                    if pnl > 0:
                        stats["wins"] += 1
                        win_reg.record("preferred_pair", f"{pref}/{common}",
                                       d, nd, qty,
                                       int(pref_by_d[d].close + common_by_d[d].close),
                                       int(pref_by_d[nd].close + common_by_d[nd].close))
                    elif pnl < 0:
                        stats["losses"] += 1
                    break
    return stats


def simulate_crash_recovery(bars_by_symbol: dict[str, list[Bar]], bar_store,
                             win_reg: WinningTradesRegistry,
                             panic_drawdown_pct: float = -2.0,
                             recovery_target_pct: float = 5.0,
                             stop_pct: float = 3.0) -> dict:
    """Sec 23 CrashRecovery: KOSPI 일봉 일일변동 ≤ -2% → panic state.
    그 다음날 시초가에 universe 의 모든 종목 매수, +5% 익절 / -3% 손절."""
    stats = dict(trades=0, wins=0, losses=0, total_pnl=0.0)
    kospi_bars = list(bar_store.read("KOSPI", "1d"))
    if not kospi_bars:
        return stats
    panic_dates = []
    for i in range(1, len(kospi_bars)):
        prev = kospi_bars[i - 1]
        cur = kospi_bars[i]
        if prev.close == 0:
            continue
        change_pct = (cur.close - prev.close) / prev.close * 100
        if change_pct <= panic_drawdown_pct:
            panic_dates.append(cur.timestamp.date())

    for symbol, bars in bars_by_symbol.items():
        bars_by_d = {b.timestamp.date(): b for b in bars}
        for pd in panic_dates:
            # entry next trading day after panic
            future = [b for b in bars if b.timestamp.date() > pd][:1]
            if not future:
                continue
            nb = future[0]
            entry = nb.open
            tp = entry * (1 + recovery_target_pct / 100)
            sl = entry * (1 - stop_pct / 100)
            if nb.low <= sl:
                exit_price = int(sl)  # 보수적: 일봉 high+low 동시 hit 시 손절 먼저 가정
            elif nb.high >= tp:
                exit_price = int(tp)
            else:
                exit_price = nb.close
            qty = 10
            pnl = (exit_price - entry) * qty
            stats["trades"] += 1
            stats["total_pnl"] += pnl
            win_reg.record("crash_recovery", symbol, pd, nb.timestamp.date(),
                               qty, entry, exit_price)
            if pnl > 0:
                stats["wins"] += 1
            elif pnl < 0:
                stats["losses"] += 1
    return stats


# ============================================================================
# V3 분봉 기반 simulators — 5/8 (금) 1일치 분봉으로 단일 일자 시뮬
# ============================================================================


def _conservative_exit(entry: int, future_bars: list[Bar], tp_pct: float,
                        sl_pct: float, max_minutes: int = 0) -> tuple[int, str, int]:
    """tick-level not available → 분봉 high/low 로 exit 판정. 보수적 (sl 먼저).
    max_minutes > 0 면 시간 청산 추가. 반환 (exit_price, reason, idx)."""
    tp = entry * (1 + tp_pct / 100)
    sl = entry * (1 - sl_pct / 100)
    for i, b in enumerate(future_bars):
        if max_minutes and i >= max_minutes:
            return (b.close, "timeout", i)
        if b.low <= sl:
            return (int(sl), "sl", i)
        if b.high >= tp:
            return (int(tp), "tp", i)
    if future_bars:
        return (future_bars[-1].close, "eod", len(future_bars) - 1)
    return (entry, "no_data", 0)


def simulate_opening_momentum_minute(bars_by_symbol: dict[str, list[Bar]],
                                       win_reg: WinningTradesRegistry,
                                       surge_pct: float = 5.0,
                                       take_profit_pct: float = 3.0) -> dict:
    """G OpeningMomentum 정밀 분봉: 09:00 first bar open 기록, 09:03-09:25
    분봉 close 가 +5% 이상이면 entry. 09:50 강제 청산. lookahead 없음."""
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
    stats = dict(trades=0, wins=0, losses=0, total_pnl=0.0)
    for symbol, bars in bars_by_symbol.items():
        if not bars:
            continue
        first = bars[0]
        open_price = first.open
        if open_price <= 0:
            continue
        entered_at: int | None = None
        entry_price = 0
        for i, b in enumerate(bars):
            local_t = b.timestamp.astimezone(KST).time()
            if local_t.hour < 9 or (local_t.hour == 9 and local_t.minute < 3):
                continue
            if local_t.hour > 9 or (local_t.hour == 9 and local_t.minute > 25):
                break
            change_pct = (b.close - open_price) / open_price * 100
            if change_pct >= surge_pct:
                entered_at = i
                entry_price = b.close
                break
        if entered_at is None:
            continue
        exit_price = 0
        for j in range(entered_at + 1, len(bars)):
            b = bars[j]
            local_t = b.timestamp.astimezone(KST).time()
            if local_t.hour > 9 or (local_t.hour == 9 and local_t.minute >= 50):
                exit_price = b.open
                break
            tp = entry_price * (1 + take_profit_pct / 100)
            if b.low <= entry_price:
                exit_price = entry_price
                break
            if b.high >= tp:
                exit_price = int(tp)
                break
        if exit_price == 0:
            exit_price = bars[-1].close
        qty = 10
        pnl = (exit_price - entry_price) * qty
        stats["trades"] += 1
        stats["total_pnl"] += pnl
        if pnl > 0:
            stats["wins"] += 1
            win_reg.record("opening_momentum_min", symbol,
                           bars[entered_at].timestamp.date(),
                           bars[entered_at].timestamp.date(),
                           qty, entry_price, exit_price)
        elif pnl < 0:
            stats["losses"] += 1
    return stats


def simulate_vwap_reversion_minute(bars_by_symbol: dict[str, list[Bar]],
                                     win_reg: WinningTradesRegistry,
                                     entry_sigma: float = 1.5,
                                     stop_sigma: float = 2.5,
                                     volume_spike_x: float = 3.0) -> dict:
    """F VWAP 회귀 분봉: running VWAP + rolling σ. close 가 vwap-1.5σ + 거래량
    spike → entry. 회귀 또는 -2.5σ 손절."""
    import statistics
    stats = dict(trades=0, wins=0, losses=0, total_pnl=0.0)
    for symbol, bars in bars_by_symbol.items():
        if len(bars) < 30:
            continue
        sum_pv = 0.0
        sum_v = 0.0
        prices: list[int] = []
        recent_vols: list[int] = []
        in_pos = False
        entry_price = 0
        entry_idx = 0
        for i, b in enumerate(bars):
            sum_pv += b.close * b.volume
            sum_v += b.volume
            prices.append(b.close)
            recent_vols.append(b.volume)
            if len(prices) > 60:
                prices.pop(0)
            if len(recent_vols) > 30:
                recent_vols.pop(0)
            if sum_v <= 0 or len(prices) < 30:
                continue
            vwap = sum_pv / sum_v
            sigma = statistics.pstdev(prices) if len(prices) > 1 else 0
            if sigma <= 0 or vwap <= 0:
                continue
            if in_pos:
                if b.close >= vwap:
                    qty = 10
                    pnl = (b.close - entry_price) * qty
                    stats["trades"] += 1
                    stats["total_pnl"] += pnl
                    if pnl > 0:
                        stats["wins"] += 1
                        win_reg.record("vwap_reversion_min", symbol,
                                       bars[entry_idx].timestamp.date(),
                                       b.timestamp.date(), qty, entry_price, b.close)
                    elif pnl < 0:
                        stats["losses"] += 1
                    in_pos = False
                else:
                    deviation = (b.close - vwap) / sigma
                    if deviation <= -stop_sigma:
                        qty = 10
                        pnl = (b.close - entry_price) * qty
                        stats["trades"] += 1
                        stats["total_pnl"] += pnl
                        if pnl < 0:
                            stats["losses"] += 1
                        in_pos = False
            else:
                deviation = (b.close - vwap) / sigma
                if deviation > -entry_sigma:
                    continue
                recent5 = recent_vols[-5:]
                prior = recent_vols[:-5]
                if not prior:
                    continue
                if sum(recent5) / 5 < (sum(prior) / len(prior)) * volume_spike_x:
                    continue
                in_pos = True
                entry_price = b.close
                entry_idx = i
    return stats


def simulate_scalping_minute(bars_by_symbol: dict[str, list[Bar]],
                              win_reg: WinningTradesRegistry,
                              spike_pct: float = 0.5,
                              tp_pct: float = 0.5,
                              sl_pct: float = 0.3,
                              max_hold_min: int = 5) -> dict:
    """E 스캘핑 분봉: 09:00-09:50 시간대 분봉 spike + 거래량 ×3 → next bar open
    entry. tp +0.5%, sl -0.3%, max 5분 hold."""
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
    stats = dict(trades=0, wins=0, losses=0, total_pnl=0.0)
    for symbol, bars in bars_by_symbol.items():
        if len(bars) < 10:
            continue
        for i in range(5, len(bars) - 1):
            b = bars[i]
            local_t = b.timestamp.astimezone(KST).time()
            if local_t.hour > 9 or (local_t.hour == 9 and local_t.minute > 50):
                break
            if local_t.hour < 9:
                continue
            if b.open <= 0:
                continue
            spike = (b.high - b.open) / b.open * 100
            if spike < spike_pct:
                continue
            avg_vol = sum(b2.volume for b2 in bars[max(0, i - 5):i]) / 5
            if avg_vol == 0 or b.volume < avg_vol * 3:
                continue
            entry_price = bars[i + 1].open
            future = bars[i + 1: i + 1 + max_hold_min]
            exit_price, _, _ = _conservative_exit(entry_price, future, tp_pct, sl_pct, max_hold_min)
            qty = 10
            pnl = (exit_price - entry_price) * qty
            stats["trades"] += 1
            stats["total_pnl"] += pnl
            win_reg.record("scalping_min", symbol,
                               b.timestamp.date(), bars[i + 1].timestamp.date(),
                               qty, entry_price, exit_price)
            if pnl > 0:
                stats["wins"] += 1
            elif pnl < 0:
                stats["losses"] += 1
    return stats


def simulate_pair_follow_minute(reg, bar_store, win_reg: WinningTradesRegistry,
                                 start, end, take_profit_pct: float = 2.5,
                                 stop_loss_pct: float = 1.5,
                                 hold_minutes: int = 5) -> dict:
    """A 짝꿍 분봉 정밀: 시총 desc 정렬에서 인접 두 종목 (1→2, 3→4, ...)
    을 leader/follower 로 단순 매핑 (theme_of 외부 데이터 부재). leader 분봉
    high 가 prev_day_close × 1.30 (상한가 추정) 도달 시 follower next bar
    open BUY. 5분 hold + tp/sl."""
    stats = dict(trades=0, wins=0, losses=0, total_pnl=0.0)
    universe = reg.top_by_market_cap(200, market="KOSDAQ")
    sorted_caps = sorted(universe, key=lambda x: x.market_cap_krw or 0, reverse=True)
    pairs = {sorted_caps[i].code: sorted_caps[i + 1].code
             for i in range(0, len(sorted_caps) - 1, 2)}
    for leader_code, follower_code in pairs.items():
        leader_bars = list(bar_store.read(leader_code, "1m", start=start, end=end))
        follower_bars = list(bar_store.read(follower_code, "1m", start=start, end=end))
        if not leader_bars or not follower_bars:
            continue
        leader_d = list(bar_store.read(leader_code, "1d"))
        if len(leader_d) < 2:
            continue
        prev_close = leader_d[-2].close
        limit_up_price = int(prev_close * 1.30)
        for i, lb in enumerate(leader_bars):
            if lb.high < limit_up_price:
                continue
            entry_idx = None
            for j, fb in enumerate(follower_bars):
                if fb.timestamp > lb.timestamp:
                    entry_idx = j
                    break
            if entry_idx is None:
                break
            entry_price = follower_bars[entry_idx].open
            future = follower_bars[entry_idx + 1: entry_idx + 1 + hold_minutes]
            exit_price, reason, _ = _conservative_exit(
                entry_price, future, take_profit_pct, stop_loss_pct, hold_minutes
            )
            qty = 10
            pnl = (exit_price - entry_price) * qty
            stats["trades"] += 1
            stats["total_pnl"] += pnl
            win_reg.record("pair_follow_min", follower_code,
                               lb.timestamp.date(),
                               follower_bars[entry_idx].timestamp.date(),
                               qty, entry_price, exit_price)
            if pnl > 0:
                stats["wins"] += 1
            elif pnl < 0:
                stats["losses"] += 1
            break
    return stats


# ============================================================================
# Original opening_momentum (lookahead bias 일봉 기반) — disabled
# ============================================================================


def _legacy_simulate_opening_momentum(bars_by_symbol: dict[str, list[Bar]], win_reg: WinningTradesRegistry,
                               surge_pct: float = 5.0, take_profit_pct: float = 3.0) -> dict:
    """일봉 기반 단순 시뮬레이션 (DISABLED — lookahead bias). 분봉 V3 사용.
    open→high 변동 ≥ 5% 이면 entry, 동일 일봉
    내 +3% 도달 가정 (high 까지 도달했다면 익절). 일봉 방향이 음봉이면 손절.
    실 strategy (분봉 09:03-09:25) 와 다른 단순화."""
    stats = dict(trades=0, wins=0, losses=0, total_pnl=0.0)
    for symbol, bars in bars_by_symbol.items():
        for b in bars:
            if b.open <= 0:
                continue
            day_surge = (b.high - b.open) / b.open * 100
            if day_surge < surge_pct:
                continue
            entry = int(b.open * (1 + surge_pct / 100))
            tp = entry * (1 + take_profit_pct / 100)
            sl = entry  # 매수가 정확 hit 시 손절 (실 strategy)
            if b.high >= tp:
                exit_price = int(tp)
            elif b.low <= sl:
                exit_price = int(sl)
            else:
                exit_price = b.close
            qty = 10
            pnl = (exit_price - entry) * qty
            stats["trades"] += 1
            stats["total_pnl"] += pnl
            win_reg.record("opening_momentum", symbol, b.timestamp.date(),
                               b.timestamp.date(), qty, entry, exit_price)
            if pnl > 0:
                stats["wins"] += 1
            elif pnl < 0:
                stats["losses"] += 1
    return stats


# -------- Main runner ---------------------------------------------------


def load_bars(bar_store: BarStore, codes: list[str], start: datetime, end: datetime,
              timeframe: str = "1d") -> dict[str, list[Bar]]:
    out = {}
    for c in codes:
        bars = list(bar_store.read(c, timeframe, start=start, end=end))
        if bars:
            out[c] = bars
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--universe-db", default="data/universe.sqlite")
    parser.add_argument("--win-db", default="data/winning_trades.sqlite")
    parser.add_argument("--strategies", nargs="*",
                        default=[
                            # 일봉 이상 (사용자 결정 2026-05-10)
                            "closing_bet",        # D/I 도지 종가베팅 (스윙: overnight 1일)
                            "bottom_volume_spike",  # K 바닥 거래량 (스윙: 수일)
                            "breakout",           # C 돌파 (단타)
                            "microcap",           # Sec 14 소형주 (단타~스윙)
                            "preferred_pair",     # H 우선주페어 (스윙: 수일)
                            "crash_recovery",     # Sec 23 패닉 회복 (스윙: 수일)
                            # V3 분봉 기반 (5/8 1일치 분봉 사용)
                            "opening_momentum_min",  # G 시초가 모멘텀 (단타) 09:03-09:25
                            "vwap_reversion_min",    # F VWAP 회귀 (단타)
                            "scalping_min",          # E 스캘핑 09:00-09:50
                            "pair_follow_min",       # A 짝꿍 (스캘핑) 분봉
                        ],
                        help="strategies to simulate (분봉 V3 + 일봉 V2)")
    args = parser.parse_args()

    end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=args.days + 30)  # 60-day lookback ensures K window

    reg = UniverseRegistry(args.universe_db)
    win_reg = WinningTradesRegistry(args.win_db)
    bar_store = BarStore(args.data_dir)

    print(f"=== Historical emulation: 2-week window ({start.date()} ~ {end.date()}) ===")

    for strategy in args.strategies:
        spec = STRATEGY_SPECS.get(strategy, {})
        print(f"\n--- Strategy: {strategy} ---")
        if spec:
            print(f"  data: universe={spec.get('uni')}  tf={spec.get('tf')}  rule={spec.get('window')}")
        universe = choose_universe_for_strategy(strategy, reg)
        codes = [e.code for e in universe]
        print(f"  Universe: {len(codes)} symbols")
        if not codes:
            print(f"  (empty universe, skip)")
            continue

        t0 = time.monotonic()
        bars_by_symbol = load_bars(bar_store, codes, start, end)
        print(f"  Loaded bars for {len(bars_by_symbol)} symbols in {time.monotonic() - t0:.1f}s")

        if strategy == "closing_bet":
            stats = simulate_doji_closing_bet(bars_by_symbol, win_reg)
        elif strategy == "opening_momentum":
            print("  ⚠ opening_momentum 일봉 simulator 는 lookahead bias (high vs open 순서 모름) → 분봉 도착 후 정밀 V2. 일단 skip.")
            continue
        elif strategy == "bottom_volume_spike":
            stats = simulate_60day_low_recovery(bars_by_symbol, win_reg)
        elif strategy == "breakout":
            stats = simulate_breakout(bars_by_symbol, win_reg)
        elif strategy == "microcap":
            stats = simulate_microcap(bars_by_symbol, win_reg)
        elif strategy == "preferred_pair":
            stats = simulate_preferred_pair(reg, bar_store, win_reg, start, end)
        elif strategy == "crash_recovery":
            stats = simulate_crash_recovery(bars_by_symbol, bar_store, win_reg)
        elif strategy == "opening_momentum_min":
            # V3 분봉 simulator: 1m timeframe load
            min_bars_by_symbol = load_bars(bar_store, codes, start, end, timeframe="1m")
            stats = simulate_opening_momentum_minute(min_bars_by_symbol, win_reg)
        elif strategy == "vwap_reversion_min":
            min_bars_by_symbol = load_bars(bar_store, codes, start, end, timeframe="1m")
            stats = simulate_vwap_reversion_minute(min_bars_by_symbol, win_reg)
        elif strategy == "scalping_min":
            min_bars_by_symbol = load_bars(bar_store, codes, start, end, timeframe="1m")
            stats = simulate_scalping_minute(min_bars_by_symbol, win_reg)
        elif strategy == "pair_follow_min":
            stats = simulate_pair_follow_minute(reg, bar_store, win_reg, start, end)
        else:
            print(f"  (no V1 simulator implemented for {strategy})")
            continue

        win_rate = stats["wins"] / stats["trades"] if stats["trades"] else 0.0
        print(
            f"  → trades={stats['trades']:5d}  wins={stats['wins']:5d}  "
            f"losses={stats['losses']:5d}  win_rate={win_rate*100:.1f}%  "
            f"total_pnl={stats['total_pnl']:+,.0f} KRW"
        )

    print("\n=== Final summary (all trades, wins + losses + net) ===")
    summary = win_reg.summary()
    if not summary:
        print("  (no trades recorded)")
    else:
        hdr = (f"  {'strategy':<22} {'tf':<8} {'trades':>6} {'wins':>5} {'loss':>5} "
               f"{'win%':>5} {'win_krw':>13} {'loss_krw':>13} {'NET':>13}")
        print(hdr)
        print("  " + "─" * (len(hdr) - 2))
        rows = sorted(summary.items(), key=lambda kv: kv[1]["net_krw"], reverse=True)
        net_total = 0
        for s, st in rows:
            net_total += st["net_krw"]
            spec = STRATEGY_SPECS.get(s, {})
            tf = spec.get("tf", "?")
            print(
                f"  {s:<22} {tf:<8} {st['trades']:>6d} {st['wins']:>5d} {st['losses']:>5d} "
                f"{st['win_rate']*100:>4.0f}% "
                f"{st['win_krw']:>+13,.0f} {st['loss_krw']:>+13,.0f} "
                f"{st['net_krw']:>+13,.0f}"
            )
        print("  " + "─" * (len(hdr) - 2))
        print(f"  {'TOTAL NET':<22} {'':<8} {'':>6} {'':>5} {'':>5} {'':>5} {'':>13} {'':>13} "
              f"{net_total:>+13,.0f}")
        print()
        print("  data sources per strategy:")
        for s in summary:
            spec = STRATEGY_SPECS.get(s, {})
            print(f"    {s:<22} universe={spec.get('uni','?')}  tf={spec.get('tf','?')}")
            print(f"    {'':<22}   rule={spec.get('window','?')}")

    win_reg.close()
    reg.close()
    print("\nAll trades stored at:", args.win_db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
