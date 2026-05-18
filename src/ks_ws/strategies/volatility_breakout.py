"""VolatilityBreakoutStrategy — Larry Williams 변동성 돌파.

technical_strategy.md 룰 추가. 알고리즘:
- 매일 시가 (open) + k * (전일 high - low) 의 trigger price 산출 (k=0.5 default).
- 장중 분봉 / tick 이 trigger 위로 cross 하면 BUY.
- 청산: 동일 거래일 종가 또는 TP/SL.

본 V1 = tick 기반:
- 시작 시점에 종목별 (전일 high, 전일 low) 외부 주입 (compute_prev_hl).
- 처음 들어오는 tick 의 가격 = 그날 open 으로 가정 (장 시작 후 process 시작 시).
  같은 KST date 의 첫 tick price 를 ``open`` 으로 저장.
- 동일 일자 1회 진입. TP/SL/timeout exit, force-close 안 함.

Position sizing 은 Allocator 가 결정.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ks_ws.domain import Bar, Side, Signal, Tick

# 분봉 거래대금 = bar.value 가 누적 (CYBOS 데이터 quirk). on_bar 에서 LAG diff 로
# 분당 값 계산 — 종목 + 일자별 마지막 cumulative 보관.
from ks_ws.storage.bars import BarStore
from ks_ws.storage.trade_review import TradeReview, TradeReviewLog
from ks_ws.strategies.base import Strategy

_KST = ZoneInfo("Asia/Seoul")


@dataclass
class _Pos:
    entry: int
    entry_time: datetime
    tp_price: int | None = None
    sl_price: int | None = None
    # Anchor-based trailing (사용자 룰 2026-05-15 Tier 3):
    # 처음 entry × (1 + activation_pct) 도달 후 max_seen 추적 시작.
    # max_seen × (1 - trail_pct) 이탈 시 SELL.
    max_seen: int = 0
    trail_active: bool = False


class VolatilityBreakoutStrategy(Strategy):
    name = "volatility_breakout"
    style = "day_trade"  # 사용자 룰 (2026-05-15)

    def __init__(
        self,
        *,
        prev_high_low: dict[str, tuple[int, int]],
        k: float = 0.5,
        take_profit_pct: float = 3.0,
        stop_loss_pct: float = 2.0,
        max_hold_minutes: int = 240,  # 단타 (사용자 doc)
        confidence: float = 0.65,
        review_log: TradeReviewLog | None = None,
        atr_provider=None,
        # Tier 3 trailing (사용자 룰 2026-05-15)
        trailing_activation_pct: float = 1.5,  # entry × (1 + N%) 도달 후 trailing 시작
        trailing_pct: float = 1.0,  # max_seen × (1 - N%) 이탈 시 SELL
        # 거래대금 + turnover + RVOL entry filter (사용자 룰 2026-05-18)
        volume_filter=None,
        # MTF + 시간대 + KOSPI regime gate (사용자 룰 2026-05-18, 승률 ↑)
        entry_gate=None,
        # 상한가 즉시 청산 (사용자 룰 2026-05-18: vb=당일청산이라 상한가 lock 더 hold 무의미)
        prev_close: dict[str, int] | None = None,
        limit_up_pct: float = 29.7,  # +29.7% 도달 = 사실상 상한가 (호가 단위 buffer 2tick)
        # 사용자 룰 5/18 (D): lookahead bias fix.
        # daily_history 있으면 entry_ts 기준 *전일* 일봉 사용 (정확한 backtest).
        # 미설정 시 prev_high_low / prev_close dict (legacy = latest bar) 사용.
        daily_history: dict[str, list] | None = None,
    ) -> None:
        if not 0 < k < 2:
            raise ValueError("k must be in (0, 2)")
        if take_profit_pct <= 0 or stop_loss_pct <= 0:
            raise ValueError("pct must be positive")
        if not 0 < confidence <= 1:
            raise ValueError("confidence in (0, 1]")
        if trailing_activation_pct < 0 or trailing_pct <= 0:
            raise ValueError("trailing pct must be non-negative / positive")
        self.prev_hl = dict(prev_high_low)
        self.k = k
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_hold = timedelta(minutes=max_hold_minutes)
        self.confidence = confidence
        self.review_log = review_log
        self.atr_provider = atr_provider
        self.trailing_activation_pct = trailing_activation_pct
        self.trailing_pct = trailing_pct
        self.volume_filter = volume_filter
        self.entry_gate = entry_gate
        self.prev_close = dict(prev_close) if prev_close else {}
        self.limit_up_pct = limit_up_pct
        self.daily_history = daily_history or {}
        # (symbol, kst_date) → (prev_high, prev_low, prev_close) cache
        self._prev_bar_cache: dict[tuple[str, object], tuple[int, int, int] | None] = {}
        self._open: dict[str, _Pos] = {}
        # symbol → (date, open_price)
        self._day_open: dict[str, tuple[object, int]] = {}
        # symbol → (date, was_above_trigger)
        self._was_above: dict[str, tuple[object, bool]] = {}
        # same-day single entry
        self._entered_today: set[tuple[str, object]] = set()
        # 누적 value/volume 의 LAG diff 로 분당 계산 — (sym, date) → 직전 cumulative
        self._cum_prev: dict[tuple[str, object], tuple[int, int]] = {}

    def on_bar(self, bar: Bar) -> list[Signal]:
        """분봉 도착 시 volume filter rolling stats 갱신 (entry 전 신호로)."""
        if self.volume_filter is None or bar.timeframe != "1m":
            return []
        kst_date = bar.timestamp.astimezone(_KST).date()
        prev_key = (bar.symbol, kst_date)
        prev_val, prev_vol = self._cum_prev.get(prev_key, (0, 0))
        bar_val = max(0, bar.value - prev_val)
        bar_vol = max(0, bar.volume - prev_vol)
        self._cum_prev[prev_key] = (bar.value, bar.volume)
        self.volume_filter.on_bar(bar.symbol, bar_val, bar_vol)
        return []

    def _record_review(self, pos: _Pos, tick: Tick, *, exit_reason: str,
                       exit_note: str) -> None:
        if self.review_log is None:
            return
        with contextlib.suppress(Exception):
            self.review_log.record(TradeReview(
                strategy=self.name, symbol=tick.symbol,
                entry_ts=pos.entry_time, entry_price=pos.entry, qty=1,
                exit_ts=tick.timestamp, exit_price=tick.price,
                pnl_krw=tick.price - pos.entry, exit_reason=exit_reason,
                entry_note=f"vol_breakout: k={self.k} entry={pos.entry}",
                exit_note=exit_note,
            ))

    def _kst_date(self, tick: Tick) -> object:
        return tick.timestamp.astimezone(_KST).date()

    def _resolve_prev_bar(self, symbol: str, kst_date) -> tuple[int, int, int] | None:
        """ts 의 KST date 기준 직전 일봉 (high, low, close) 반환.
        daily_history 우선, 없으면 legacy prev_hl/prev_close dict (latest bar) fallback.
        """
        key = (symbol, kst_date)
        if key in self._prev_bar_cache:
            return self._prev_bar_cache[key]
        bars = self.daily_history.get(symbol, [])
        if bars:
            prev = None
            for b in bars:
                b_date = b.timestamp.astimezone(_KST).date()
                if b_date < kst_date:
                    prev = b
                else:
                    break  # bars are sorted by ts ascending
            if prev is not None:
                result = (prev.high, prev.low, prev.close)
                self._prev_bar_cache[key] = result
                return result
        # Legacy fallback (5/18 이전 인터페이스 호환 — backtest 의 latest-only bug)
        hl = self.prev_hl.get(symbol)
        pc = self.prev_close.get(symbol)
        if hl and len(hl) == 2:
            high, low = hl
            close = pc if pc and pc > 0 else 0
            result = (high, low, close)
            self._prev_bar_cache[key] = result
            return result
        self._prev_bar_cache[key] = None
        return None

    def _trigger_price(self, symbol: str, day_open: int, kst_date=None) -> int | None:
        if kst_date is not None:
            pb = self._resolve_prev_bar(symbol, kst_date)
            if not pb:
                return None
            high, low, _ = pb
        else:
            hl = self.prev_hl.get(symbol)
            if not hl:
                return None
            high, low = hl
        if high <= 0 or low <= 0 or high <= low:
            return None
        return int(day_open + self.k * (high - low))

    def on_tick(self, tick: Tick) -> list[Signal]:
        kst_date = self._kst_date(tick)

        # Track the day open (first tick of the calendar day in KST)
        day_open_rec = self._day_open.get(tick.symbol)
        if day_open_rec is None or day_open_rec[0] != kst_date:
            self._day_open[tick.symbol] = (kst_date, tick.price)
        day_open = self._day_open[tick.symbol][1]

        # Exit check first
        pos = self._open.get(tick.symbol)
        if pos is not None:
            tp = pos.tp_price if pos.tp_price is not None else pos.entry * (1 + self.take_profit_pct / 100)
            sl = pos.sl_price if pos.sl_price is not None else pos.entry * (1 - self.stop_loss_pct / 100)
            # Tier 3 anchor-based trailing 활성/갱신
            if tick.price > pos.max_seen:
                pos.max_seen = tick.price
            activation_price = pos.entry * (1 + self.trailing_activation_pct / 100)
            if not pos.trail_active and pos.max_seen >= activation_price:
                pos.trail_active = True
            # 상한가 즉시 청산 — vb 는 당일청산 룰이라 상한가 lock 시 더 hold 무의미
            # entry_ts 기준 직전 일봉 close 사용 (lookahead bias fix)
            pb_for_limit = self._resolve_prev_bar(tick.symbol, kst_date)
            pc = pb_for_limit[2] if pb_for_limit else 0
            if pc and pc > 0 and tick.price >= pc * (1 + self.limit_up_pct / 100):
                del self._open[tick.symbol]
                self._record_review(pos, tick, exit_reason="limit_up",
                                    exit_note=f"limit_up @ {tick.price} (prev_close={pc})")
                return [self._sig(tick, Side.SELL,
                                  note=f"limit_up @ {tick.price} (+{(tick.price/pc-1)*100:.1f}%)")]
            if tick.price >= tp:
                del self._open[tick.symbol]
                self._record_review(pos, tick, exit_reason="TP",
                                    exit_note=f"TP @ {tick.price}")
                return [self._sig(tick, Side.SELL, note=f"TP @ {tick.price}")]
            if tick.price <= sl:
                del self._open[tick.symbol]
                self._record_review(pos, tick, exit_reason="SL",
                                    exit_note=f"SL @ {tick.price}")
                return [self._sig(tick, Side.SELL, urgency="high",
                                  note=f"SL @ {tick.price}")]
            # Trailing 이탈 check (trail_active 일 때만)
            if pos.trail_active:
                trail_stop = pos.max_seen * (1 - self.trailing_pct / 100)
                if tick.price <= trail_stop:
                    del self._open[tick.symbol]
                    self._record_review(pos, tick, exit_reason="trail",
                                        exit_note=f"trail @ {tick.price} max={pos.max_seen}")
                    return [self._sig(tick, Side.SELL,
                                      note=f"trail @ {tick.price} max={pos.max_seen}")]
            if tick.timestamp - pos.entry_time >= self.max_hold:
                del self._open[tick.symbol]
                self._record_review(pos, tick, exit_reason="timeout",
                                    exit_note="hold timeout")
                return [self._sig(tick, Side.SELL, note="hold timeout")]
            return []

        # Entry check — edge detection (below → above trigger)
        trigger = self._trigger_price(tick.symbol, day_open, kst_date)
        if trigger is None:
            return []
        prev_rec = self._was_above.get(tick.symbol)
        prev_above = prev_rec[1] if (prev_rec and prev_rec[0] == kst_date) else False
        curr_above = tick.price >= trigger
        self._was_above[tick.symbol] = (kst_date, curr_above)

        if not curr_above or prev_above:
            return []
        day_key = (tick.symbol, kst_date)
        if day_key in self._entered_today:
            return []
        # Volume filter — 거래대금/turnover/RVOL 통과 시만 entry (사용자 룰 5/18)
        if self.volume_filter is not None and not self.volume_filter.passes(tick.symbol):
            return []
        # Entry gate — MTF + KOSPI regime + 시간대 (5/18 승률 ↑ 룰)
        if self.entry_gate is not None and not self.entry_gate.passes(tick.symbol, tick.timestamp):
            return []
        self._entered_today.add(day_key)
        from ks_ws.strategies._atr_helper import resolve_tp_sl
        tp_price, sl_price = resolve_tp_sl(
            tick.price, tick.symbol,
            atr_provider=self.atr_provider, style=self.style,
            fallback_tp_pct=self.take_profit_pct,
            fallback_sl_pct=self.stop_loss_pct,
        )
        self._open[tick.symbol] = _Pos(
            entry=tick.price, entry_time=tick.timestamp,
            tp_price=tp_price, sl_price=sl_price,
            max_seen=tick.price,  # 초기 max = entry
        )
        return [Signal(
            symbol=tick.symbol, side=Side.BUY, confidence=self.confidence,
            strategy=self.name, timestamp=tick.timestamp,
            note=f"vol_brk: open={day_open} trigger={trigger} k={self.k} TP={tp_price} SL={sl_price}",
        )]

    def _sig(self, tick: Tick, side: Side, *, note: str,
             urgency: str = "normal") -> Signal:
        return Signal(
            symbol=tick.symbol, side=side, confidence=1.0,
            urgency=urgency,  # type: ignore[arg-type]
            strategy=self.name, timestamp=tick.timestamp, note=note,
        )

    def open_positions(self) -> dict[str, _Pos]:
        return dict(self._open)


def compute_prev_high_low(
    bar_store: BarStore, symbols: list[str],
) -> dict[str, tuple[int, int]]:
    """Return {symbol: (prev_high, prev_low)} from BarStore 1d.
    Uses the most recent fully-closed daily bar.
    """
    out: dict[str, tuple[int, int]] = {}
    for sym in symbols:
        bars: list[Bar] = list(bar_store.read(sym, "1d"))
        if not bars:
            continue
        last = bars[-1]
        out[sym] = (last.high, last.low)
    return out


def compute_prev_close(
    bar_store: BarStore, symbols: list[str],
) -> dict[str, int]:
    """Return {symbol: prev_close} from BarStore 1d. 상한가 청산 룰의 base price."""
    out: dict[str, int] = {}
    for sym in symbols:
        bars: list[Bar] = list(bar_store.read(sym, "1d"))
        if not bars:
            continue
        out[sym] = bars[-1].close
    return out
