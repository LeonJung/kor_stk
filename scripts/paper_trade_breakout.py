"""Paper-trade live: breakout + closing-bet strategies on KIS mock account.

용도: 사용자 명시 (2026-05-11) — 가장 유리한 strategy = breakout 으로 시작,
closing_bet (도지 종가베팅, 책 strategy I) 13:30 이후 추가 가동.

흐름:
- universe = 시총상위 20 (KIS WS subscription max)
- 각 종목 60일 high (BarStore) 미리 계산
- KisMarketDataHub HOT (WS H0STCNT0) → Tick → LiveBreakoutStrategy + DojiEmitter
- DojiEmitter 가 13:30 이후 매 분 partial OHLC 기준 도지 검사 → DojiCandle 발행
- ClosingBetStrategy 가 DojiCandle event 받아 BUY signal — overnight 포지션은 ledger 영속화 →
  다음날 process 재시작 시 _hydrate_closing_bet() 가 미체결 BUY 를 _open dict 에 복원,
  다음날 첫 tick 에 entry_price 캡처 후 TP/SL 로 자동 청산.
  TP/SL 미도달 시 force-close 안 함 — 사용자 룰: 전략이 청산 조건 미달이면 hold 유지.
- BreakoutStrategy / ClosingBetStrategy → Signal emit → Allocator → Risk → KisOrderRouter (mock)
- 세션 종료 (20:00 KST, 사용자 명시 2026-05-13) 또는 KeyboardInterrupt 시 ledger 요약

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.paper_trade_breakout
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# SAFETY GUARD: 호가 (orderbook) capture 자동 중단 cutoff (KST date).
# 사용자 명시 (2026-05-12): 호가 데이터는 5/12 + 5/13 이틀만 누적, 그 이후는
# capture 안 함 (용량 폭증 방지). 매 insert 전 KST today 가 이 날짜를 넘으면
# orderbook insert skip. 더 모으려면 이 날짜를 수동으로 늘릴 것.
_ORDERBOOK_CAPTURE_END_KST = date(2026, 5, 13)

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# 노이즈 줄이기 (drop warning + AES key 매 프레임 INFO 등)
logging.getLogger("ks_ws.bus").setLevel(logging.ERROR)
logging.getLogger("ks_ws.kis.realtime").setLevel(logging.WARNING)
logging.getLogger("ks_ws.kis.crypto").setLevel(logging.WARNING)
logging.getLogger("ks_ws.market.kis_hub").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("paper_trade_breakout")

_KST = ZoneInfo("Asia/Seoul")


async def main() -> int:
    from ks_ws.bus import EventBus
    from ks_ws.config import get_settings
    from ks_ws.kis.realtime import KisRealtimeFeed
    from ks_ws.live import LiveExecutor
    from ks_ws.market.kis_hub import KisMarketDataHub
    from ks_ws.orders import KisOrderRouter
    from ks_ws.risk import EnhancedRisk, Risk
    from ks_ws.runtime import Runtime
    from ks_ws.storage.bars import BarStore
    from ks_ws.storage.ledger import Ledger
    from ks_ws.storage.universe import UniverseRegistry
    from ks_ws.sources.foreign_flow import kis_foreign_flow_fetcher
    from ks_ws.sources.macro_score import blend_macro_scores
    from ks_ws.sources.rvol import score_from_rvol
    from ks_ws.strategies.closing_bet import ClosingBetStrategy
    from ks_ws.strategies.fundamental_allocator import (
        FundamentalAllocator,
        score_from_foreign_flow_krw,
    )
    from ks_ws.strategies.gates import EntryWindowGate
    from ks_ws.strategies.live_breakout import LiveBreakoutStrategy, compute_high60
    from ks_ws.events import DojiCandle

    settings = get_settings()
    bar_store = BarStore("data")
    reg = UniverseRegistry("data/universe.sqlite")
    universe = reg.top_by_market_cap(20)  # KIS WS subscription max
    codes = [e.code for e in universe]
    reg.close()

    log.info("=== Paper trade BREAKOUT on KIS mock ===")
    log.info("env=%s, account=%s", settings.env, settings.account_cano)
    log.info("Universe (top 20 시총): %s", ", ".join(codes[:5]) + " ...")

    # 60-day high per symbol (from BarStore)
    high60 = compute_high60(bar_store, codes)
    log.info("60d high computed for %d/%d symbols", len(high60), len(codes))

    # Setup
    from ks_ws.domain import Tick
    from ks_ws.market.hub import Tier

    bus = EventBus(default_maxsize=500_000)  # 분당 ~6000 tick × 20 종목 안전
    hub = KisMarketDataHub(bus, settings)
    hub.assign_many((c, Tier.HOT) for c in codes)

    # --- Tick capture sidecar (paper_trade + 5/11-12 데이터 수집 동시) ---
    import sqlite3 as _sqlite3
    tick_db_path = Path("data/ticks.sqlite")
    tick_db_path.parent.mkdir(parents=True, exist_ok=True)
    tick_conn = _sqlite3.connect(str(tick_db_path), check_same_thread=False)
    tick_conn.executescript(
        "CREATE TABLE IF NOT EXISTS ticks ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,"
        " ts_iso TEXT NOT NULL, price INTEGER NOT NULL, volume INTEGER NOT NULL,"
        " aggressor TEXT);"
        "CREATE INDEX IF NOT EXISTS idx_ticks_sym_ts ON ticks(symbol, ts_iso);"
        "CREATE TABLE IF NOT EXISTS orderbook ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,"
        " ts_iso TEXT NOT NULL,"
        " ask_px_1 INTEGER, ask_qty_1 INTEGER,"
        " ask_px_2 INTEGER, ask_qty_2 INTEGER,"
        " ask_px_3 INTEGER, ask_qty_3 INTEGER,"
        " ask_px_4 INTEGER, ask_qty_4 INTEGER,"
        " ask_px_5 INTEGER, ask_qty_5 INTEGER,"
        " bid_px_1 INTEGER, bid_qty_1 INTEGER,"
        " bid_px_2 INTEGER, bid_qty_2 INTEGER,"
        " bid_px_3 INTEGER, bid_qty_3 INTEGER,"
        " bid_px_4 INTEGER, bid_qty_4 INTEGER,"
        " bid_px_5 INTEGER, bid_qty_5 INTEGER);"
        "CREATE INDEX IF NOT EXISTS idx_ob_sym_ts ON orderbook(symbol, ts_iso);"
    )
    tick_conn.commit()
    tick_sub = bus.subscribe(Tick, maxsize=200_000)
    from ks_ws.domain import OrderBook as _OrderBook
    # SAFETY: 호가 capture 는 _ORDERBOOK_CAPTURE_END_KST 까지만. 그 이후
    # 시작 시 subscribe 자체 X (queue 도 안 차게).
    _today_kst = datetime.now(UTC).astimezone(_KST).date()
    _ob_capture_enabled = _today_kst <= _ORDERBOOK_CAPTURE_END_KST
    if _ob_capture_enabled:
        ob_sub = bus.subscribe(_OrderBook, maxsize=200_000)
        log.info("Orderbook capture ENABLED (today %s ≤ cutoff %s)",
                 _today_kst, _ORDERBOOK_CAPTURE_END_KST)
    else:
        ob_sub = None
        log.warning("Orderbook capture DISABLED (today %s > cutoff %s) — "
                    "용량 폭증 방지 가드. _ORDERBOOK_CAPTURE_END_KST 수정 필요.",
                    _today_kst, _ORDERBOOK_CAPTURE_END_KST)
    tick_count = {"n": 0}
    ob_count = {"n": 0}

    async def _tick_logger():
        async for t in tick_sub:
            try:
                tick_conn.execute(
                    "INSERT INTO ticks (symbol, ts_iso, price, volume, aggressor) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (t.symbol, t.timestamp.isoformat(), int(t.price), int(t.volume),
                     t.aggressor.value if t.aggressor else None),
                )
                tick_count["n"] += 1
                if tick_count["n"] % 500 == 0:
                    tick_conn.commit()
            except Exception:
                pass
    _tick_task = asyncio.create_task(_tick_logger())

    async def _ob_logger():
        # OrderBook 은 KisMarketDataHub HOT tier 가 H0STASP0 → OrderBook 으로 publish.
        # top 5 단계 만 sqlite 저장. SAFETY: 매 insert 전 KST today 가 cutoff
        # 넘으면 즉시 skip (예: process 가 자정 넘어 살아있을 때).
        if ob_sub is None:
            return
        async for ob in ob_sub:
            try:
                # Date guard
                _today = datetime.now(UTC).astimezone(_KST).date()
                if _today > _ORDERBOOK_CAPTURE_END_KST:
                    continue  # silently drop after cutoff
                ap = list(ob.ask_prices)[:5] + [0] * 5
                aq = list(ob.ask_quantities)[:5] + [0] * 5
                bp = list(ob.bid_prices)[:5] + [0] * 5
                bq = list(ob.bid_quantities)[:5] + [0] * 5
                tick_conn.execute(
                    "INSERT INTO orderbook (symbol, ts_iso, "
                    "ask_px_1, ask_qty_1, ask_px_2, ask_qty_2, ask_px_3, ask_qty_3, "
                    "ask_px_4, ask_qty_4, ask_px_5, ask_qty_5, "
                    "bid_px_1, bid_qty_1, bid_px_2, bid_qty_2, bid_px_3, bid_qty_3, "
                    "bid_px_4, bid_qty_4, bid_px_5, bid_qty_5"
                    ") VALUES (?,?, ?,?, ?,?, ?,?, ?,?, ?,?, ?,?, ?,?, ?,?, ?,?, ?,?)",
                    (ob.symbol, ob.timestamp.isoformat(),
                     int(ap[0]), int(aq[0]), int(ap[1]), int(aq[1]),
                     int(ap[2]), int(aq[2]), int(ap[3]), int(aq[3]),
                     int(ap[4]), int(aq[4]),
                     int(bp[0]), int(bq[0]), int(bp[1]), int(bq[1]),
                     int(bp[2]), int(bq[2]), int(bp[3]), int(bq[3]),
                     int(bp[4]), int(bq[4])),
                )
                ob_count["n"] += 1
                if ob_count["n"] % 500 == 0:
                    tick_conn.commit()
            except Exception:
                pass
    _ob_task = asyncio.create_task(_ob_logger()) if ob_sub is not None else None

    # --- Strategy entry windows (KST) ---
    # 각 strategy 가 자기 시간대에서만 BUY entry, SELL (TP/SL/force-close) 는 항상 통과.
    # 이미 시간 넘겼으면 (window 가 과거) 새 entry 0건, 기존 포지션 청산은 정상 진행.
    # 시간 안 됐으면 (window 가 미래) entry signal 무시되며 시간 도래 시 자동 활성.
    # 사용자 명시 2026-05-13: trade 활성 08:00 ~ 20:00 KST. 08:00-09:00 = 정규장
    # 전 호가 접수 시간, 15:30 = 정규장 마감, 16:00-18:00 = 시간외 단일가, 18:00-20:00 =
    # 미장 선물 leading. KIS WS frame 은 정규장 (09:00-15:30) 활발, 나머지 시간 대부분
    # idle (mock 한계).
    BREAKOUT_WINDOW = (time(8, 0), time(14, 30))
    CLOSING_BET_WINDOW = (time(13, 30), time(15, 25))

    strategy = LiveBreakoutStrategy(
        high60=high60, take_profit_pct=2.0, stop_loss_pct=3.0, max_hold_minutes=60
    )
    closing_bet = ClosingBetStrategy(
        watchlist=set(codes),
        take_profit_pct=2.0,
        stop_loss_pct=3.0,
        confidence=0.5,
    )
    # Wrap entry windows. SELL signals (TP/SL, max_hold timeout, force-close) bypass.
    breakout_gated = EntryWindowGate(strategy, windows=[BREAKOUT_WINDOW])
    closing_bet_gated = EntryWindowGate(closing_bet, windows=[CLOSING_BET_WINDOW])

    # FundamentalAllocator: BUY signals subject to per-symbol macro_score.
    # 시작 시 RVOL (BarStore 일봉) + 외인 순매수 (KIS investor-trade-by-stock-daily,
    # 어제 영업일 데이터) blend → set_macro_score. KOSPI top 20 외인 매매 단위
    # ~수천억-수조 → strong_threshold=1조 사용.
    # 5/12 paper_trade 의 universe 변별 X 문제 해결: 약한 종목 entry 차단, 강한 종목 비중 ↑.
    from datetime import timedelta as _td
    _yest_date = (datetime.now(_KST) - _td(days=1)).strftime("%Y%m%d")
    log.info("Computing macro_scores (RVOL + 외인 %s 데이터)", _yest_date)
    allocator = FundamentalAllocator(max_position_per_symbol=10, min_score=0.5)
    macro_set = 0
    for sym in codes:
        bars = list(bar_store.read(sym, "1d"))
        if len(bars) < 6:
            continue
        yesterday_value = bars[-1].value
        prev_5d_avg = sum(b.value for b in bars[-6:-1]) / 5
        if prev_5d_avg <= 0:
            continue
        rvol = yesterday_value / prev_5d_avg
        r_score = score_from_rvol(rvol)
        # Foreign flow fetch (5/12 마지막 영업일). 실패 시 0 → neutral score 1.0
        # 으로 강한 효과 X (RVOL 단독으로 fallback).
        try:
            foreign_net = kis_foreign_flow_fetcher(sym, date_yyyymmdd=_yest_date)
        except Exception as e:
            log.warning("  foreign_flow fetch failed for %s: %s", sym, e)
            foreign_net = 0
        f_score = (
            score_from_foreign_flow_krw(foreign_net, strong_threshold_krw=1_000_000_000_000)
            if foreign_net != 0
            else 1.0
        )
        score = blend_macro_scores(r_score, f_score)
        allocator.set_macro_score(sym, score)
        macro_set += 1
        fn_str = f"{foreign_net:+,d}"
        log.info(
            "  macro %s: RVOL=%.2f(r=%.2f) foreign=%s KRW(f=%.2f) blend=%.2f",
            sym, rvol, r_score, fn_str, f_score, score,
        )
    log.info("Set macro_score for %d/%d symbols (min_score=0.5 BUY veto)", macro_set, len(codes))

    runtime = Runtime(bus, [breakout_gated, closing_bet_gated], allocator)
    log.info("Strategy entry windows (KST): breakout %s-%s, closing_bet %s-%s",
             *BREAKOUT_WINDOW, *CLOSING_BET_WINDOW)

    # --- Doji emitter (for closing_bet) ---
    # 13:30 이후 매 분, 각 종목의 그날 partial OHLC 로 도지 검사 → DojiCandle publish
    partial_ohlc: dict[str, dict[str, int]] = {}
    doji_sub = bus.subscribe(Tick, maxsize=200_000)
    fired_doji: set[str] = set()

    async def _ohlc_tracker():
        async for t in doji_sub:
            sym = t.symbol
            b = partial_ohlc.get(sym)
            if b is None:
                partial_ohlc[sym] = {"open": t.price, "high": t.price,
                                     "low": t.price, "close": t.price}
            else:
                if t.price > b["high"]:
                    b["high"] = t.price
                if t.price < b["low"]:
                    b["low"] = t.price
                b["close"] = t.price

    async def _doji_emitter():
        await asyncio.sleep(5)
        while True:
            now = datetime.now(UTC).astimezone(_KST)
            if now.time() >= time(13, 30):
                for sym, b in list(partial_ohlc.items()):
                    if sym in fired_doji:
                        continue
                    rng = b["high"] - b["low"]
                    if rng <= 0 or b["open"] <= 0:
                        continue
                    body_pct = abs(b["close"] - b["open"]) / b["open"] * 100
                    range_pct = rng / b["open"] * 100
                    # 도지: body ≤ range×0.1 (몸통이 전체 범위의 10% 이내) AND 의미있는 range
                    body_to_range = abs(b["close"] - b["open"]) / max(rng, 1)
                    if body_to_range <= 0.10 and range_pct >= 1.0:
                        log.info("DOJI %s o=%d h=%d l=%d c=%d body=%.2f%% range=%.2f%%",
                                 sym, b["open"], b["high"], b["low"], b["close"],
                                 body_pct, range_pct)
                        bus.publish(DojiCandle(
                            symbol=sym,
                            timestamp=datetime.now(UTC),
                            body_pct=body_pct,
                            range_pct=range_pct,
                            direction_hint="neutral",
                        ))
                        fired_doji.add(sym)
            await asyncio.sleep(60)

    _ohlc_task = asyncio.create_task(_ohlc_tracker())
    _doji_task = asyncio.create_task(_doji_emitter())

    # Risk + Ledger (영속 — overnight closing_bet 포지션 복원용)
    ledger_path = Path("data/paper_breakout_ledger.sqlite")
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(ledger_path)

    # --- Hydrate closing_bet open positions from ledger (overnight) ---
    def _hydrate_closing_bet() -> None:
        from collections import defaultdict
        from ks_ws.strategies.closing_bet import _Position, _ordinal
        orders_by_id = {o["order_id"]: o for o in ledger.list_orders()}
        buys: dict[str, list[dict]] = defaultdict(list)
        sells: dict[str, int] = defaultdict(int)
        for f in ledger.list_fills():
            order = orders_by_id.get(f["order_id"])
            if not order:
                continue
            srcs = (order.get("sources") or "")
            if "closing_bet" not in srcs:
                continue
            if "BUY" in f["side"].upper():
                buys[f["symbol"]].append(f)
            else:
                sells[f["symbol"]] += f["quantity"]
        restored = 0
        for sym, brows in buys.items():
            buy_qty = sum(b["quantity"] for b in brows)
            if buy_qty <= sells.get(sym, 0):
                continue  # fully closed
            last = brows[-1]
            entry_time = datetime.fromisoformat(last["filled_at"])
            closing_bet._open[sym] = _Position(
                symbol=sym,
                entry_time=entry_time,
                entry_price=0,
                open_day=_ordinal(entry_time),
            )
            restored += 1
            log.info("Restored closing_bet position: %s (filled %s)", sym, last["filled_at"])
        if restored:
            log.info("Closing_bet hydrated: %d overnight positions", restored)

    _hydrate_closing_bet()

    enhanced = EnhancedRisk(
        risk=Risk(max_position_per_symbol=10, daily_loss_limit_krw=5_000_000)
    )
    router = KisOrderRouter(settings)
    executor = LiveExecutor(bus, enhanced, router, ledger=ledger)

    # Start hub + runtime + executor
    await hub.start()
    await runtime.start()
    await executor.start()

    log.info("Live trading started; awaiting session stop 20:00 KST...")

    # Stop at 20:00 KST today (사용자 명시 2026-05-13: 시간외 + 미장 선물 leading 시간대까지)
    now_kst = datetime.now(UTC).astimezone(_KST)
    market_close = now_kst.replace(hour=20, minute=0, second=0, microsecond=0)
    if market_close <= now_kst:
        log.error("Session already past 20:00 KST today.")
        return 0
    seconds_left = (market_close - now_kst).total_seconds()
    log.info("Will run for ~%.0f minutes until 20:00 KST", seconds_left / 60)

    # Periodic status loop
    # 룰 (사용자 명시 2026-05-11): 전략이 청산 조건 미달 = hold 유지. force-close 안 함.
    # closing_bet 의 next-day TP/SL 못 trigger → 다음날 process 가 ledger 에서 hydrate 해서
    # 계속 보유 (무한 hold 가능). max_hold 같은 시간 limit 은 strategy 자체 책임.
    try:
        while True:
            await asyncio.sleep(60)
            now = datetime.now(UTC).astimezone(_KST)
            if now >= market_close:
                break
            sub = len(executor.submitted)
            rej = len(executor.rejected_by_risk)
            pos = executor.positions
            opens = strategy.open_positions()
            cb_opens = closing_bet.open_positions()
            log.info("[%s] submitted=%d rejected=%d ticks=%d ob=%d brk_open=%s cb_open=%s doji_fired=%d",
                     now.strftime("%H:%M:%S"), sub, rej, tick_count["n"], ob_count["n"],
                     list(opens), list(cb_opens), len(fired_doji))
            if opens:
                for sym, p in opens.items():
                    log.info("  → %s entry=%d at %s", sym, p.entry, p.entry_time.astimezone(_KST).strftime("%H:%M:%S"))
    except KeyboardInterrupt:
        log.info("Interrupted; shutting down")

    # Shutdown
    tick_sub.close()
    if ob_sub is not None:
        ob_sub.close()
    doji_sub.close()
    _tasks_to_cancel = [_tick_task, _ohlc_task, _doji_task]
    if _ob_task is not None:
        _tasks_to_cancel.append(_ob_task)
    for t in _tasks_to_cancel:
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    tick_conn.commit()
    tick_conn.close()
    log.info("Capture flushed: %d ticks, %d orderbook events", tick_count["n"], ob_count["n"])

    await executor.stop()
    await hub.stop()

    log.info("=== Final ledger ===")
    orders = ledger.list_orders()
    fills = ledger.list_fills()
    log.info("orders=%d fills=%d positions=%d", len(orders), len(fills), len(ledger.list_positions()))
    for f in fills[-20:]:
        log.info("  fill: %s %s qty=%d @ %d  at=%s",
                 f["symbol"], f["side"], f["quantity"], f["price"], f["filled_at"])
    ledger.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
