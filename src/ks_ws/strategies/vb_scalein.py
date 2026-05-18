"""VBScaleInStrategy — vb 기반 분할매수/매도 응용 (vb 코드 X 건드림).

vb 의 trigger 룰 (Larry Williams: open + k×(전일 H-L)) 그대로 차용하되, 진입과
청산을 다단계로 분할:

진입 (default 3 단계, 합 1.0):
    E1 = trigger cross @ initial_entry → 0.50
    E2 = initial_entry × 1.005 (+0.5%) → 0.30
    E3 = initial_entry × 1.010 (+1.0%) → 0.20  ← 이 단계서 anchor trailing 무장

청산 (default 3 단계):
    TP1 = initial_entry × 1.020 (+2.0%) → 0.33 청산 + SL→BE ratchet
    TP2 = initial_entry × 1.030 (+3.0%) → 0.33 청산
    잔여 0.34 = anchor trailing (max_seen × (1 - trailing_pct))
    SL  = initial_entry × 0.985 (BE 전) / avg_entry (BE 후) → 전체 청산
    timeout 240분 → 전체 청산

비중은 Signal.confidence 로 전달 — Allocator 가 magnitude × max_position
으로 quantity 산출. SymbolWeightMatrix 가 BUY 시 추가 곱 (Tier 5).

vb 와 동일하게 same-day 1회 진입, edge detect (below → above), trigger
가격은 vb 의 룰 그대로 (compute_prev_high_low 는 vb 모듈 helper 그대로 재사용).

Position sizing 은 Allocator 가 결정. 본 strategy 는 단계별 비중 (frac)을
Signal.confidence 로 emit 만 함.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ks_ws.domain import Bar, Side, Signal, Tick
from ks_ws.storage.trade_review import TradeReview, TradeReviewLog
from ks_ws.strategies.base import Strategy

_KST = ZoneInfo("Asia/Seoul")

DEFAULT_ENTRY_PLAN: list[tuple[float, float]] = [
    (0.000, 0.50),
    (0.005, 0.30),
    (0.010, 0.20),
]
DEFAULT_EXIT_PLAN: list[tuple[float, float]] = [
    (0.020, 1.0 / 3),
    (0.030, 1.0 / 3),
]


@dataclass
class _ScalePos:
    initial_entry: int
    avg_entry: int
    entry_time: datetime
    last_entry_time: datetime
    entries: list[tuple[int, datetime, float]] = field(default_factory=list)
    buy_levels_hit: int = 0
    sell_levels_hit: int = 0
    qty_frac_in_pos: float = 0.0
    cum_bought: float = 0.0
    cum_sold: float = 0.0
    max_seen: int = 0
    trail_armed: bool = False
    sl_at_be: bool = False


class VBScaleInStrategy(Strategy):
    name = "vb_scalein"
    style = "day_trade"

    def __init__(
        self,
        *,
        prev_high_low: dict[str, tuple[int, int]],
        k: float = 0.5,
        entry_plan: list[tuple[float, float]] | None = None,
        exit_plan: list[tuple[float, float]] | None = None,
        stop_loss_pct: float = 1.5,
        max_hold_minutes: int = 240,
        trailing_pct: float = 1.0,
        review_log: TradeReviewLog | None = None,
        volume_filter=None,
        entry_gate=None,
        prev_close: dict[str, int] | None = None,
        limit_up_pct: float = 29.7,  # 상한가 즉시 청산 (vb=당일청산 룰)
        daily_history: dict[str, list] | None = None,  # lookahead fix
    ) -> None:
        if not 0 < k < 2:
            raise ValueError("k must be in (0, 2)")
        if stop_loss_pct <= 0:
            raise ValueError("stop_loss_pct must be positive")
        if trailing_pct <= 0:
            raise ValueError("trailing_pct must be positive")
        ep = list(entry_plan if entry_plan is not None else DEFAULT_ENTRY_PLAN)
        xp = list(exit_plan if exit_plan is not None else DEFAULT_EXIT_PLAN)
        if not ep:
            raise ValueError("entry_plan must be non-empty")
        if abs(sum(f for _, f in ep) - 1.0) > 1e-6:
            raise ValueError("entry_plan fractions must sum to 1.0")
        if sum(f for _, f in xp) > 1.0 + 1e-6:
            raise ValueError("exit_plan fractions must sum to ≤ 1.0")
        self.prev_hl = dict(prev_high_low)
        self.k = k
        self.entry_plan = ep
        self.exit_plan = xp
        self.stop_loss_pct = stop_loss_pct
        self.max_hold = timedelta(minutes=max_hold_minutes)
        self.trailing_pct = trailing_pct
        self.review_log = review_log
        self.volume_filter = volume_filter
        self.entry_gate = entry_gate
        self.prev_close = dict(prev_close) if prev_close else {}
        self.limit_up_pct = limit_up_pct
        self.daily_history = daily_history or {}
        self._prev_bar_cache: dict[tuple[str, object], tuple[int, int, int] | None] = {}
        self._open: dict[str, _ScalePos] = {}
        self._day_open: dict[str, tuple[object, int]] = {}
        self._was_above: dict[str, tuple[object, bool]] = {}
        self._entered_today: set[tuple[str, object]] = set()
        self._cum_prev: dict[tuple[str, object], tuple[int, int]] = {}

    def on_bar(self, bar: Bar) -> list[Signal]:
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

    def _kst_date(self, tick: Tick) -> object:
        return tick.timestamp.astimezone(_KST).date()

    def _resolve_prev_bar(self, symbol: str, kst_date) -> tuple[int, int, int] | None:
        """entry_ts 기준 직전 일봉 (high, low, close). daily_history 우선."""
        key = (symbol, kst_date)
        if key in self._prev_bar_cache:
            return self._prev_bar_cache[key]
        bars = self.daily_history.get(symbol, [])
        if bars:
            prev = None
            for b in bars:
                if b.timestamp.astimezone(_KST).date() < kst_date:
                    prev = b
                else:
                    break
            if prev is not None:
                self._prev_bar_cache[key] = (prev.high, prev.low, prev.close)
                return self._prev_bar_cache[key]
        # Legacy fallback (latest-only dict)
        hl = self.prev_hl.get(symbol)
        pc = self.prev_close.get(symbol)
        if hl and len(hl) == 2:
            high, low = hl
            self._prev_bar_cache[key] = (high, low, pc if pc and pc > 0 else 0)
            return self._prev_bar_cache[key]
        self._prev_bar_cache[key] = None
        return None

    def _trigger_price(self, symbol: str, day_open: int, kst_date=None) -> int | None:
        if kst_date is not None:
            pb = self._resolve_prev_bar(symbol, kst_date)
            if not pb:
                return None
            high, low, _ = pb
            if high <= 0 or low <= 0 or high <= low:
                return None
            return int(day_open + self.k * (high - low))
        hl = self.prev_hl.get(symbol)
        if not hl:
            return None
        high, low = hl
        if high <= 0 or low <= 0 or high <= low:
            return None
        return int(day_open + self.k * (high - low))

    def on_tick(self, tick: Tick) -> list[Signal]:
        """한 tick = 한 액션만 emit.

        Allocator combine() 가 같은 symbol BUY+SELL signal 의 net 계산을 하므로,
        한 tick 에 BUY+SELL 동시 emit 시 한쪽이 소실됨. 또한 multiple SELL
        (TP1+TP2+trail) 동시 emit 시 conf sum 으로 전체 청산되어 분할매도 효과
        사라짐. 따라서 우선순위 = SL > timeout > TP (한 단계) > trail > E2/E3
        (한 단계). 한 tick 에 정확히 0 또는 1 개 signal.
        """
        kst_date = self._kst_date(tick)
        day_open_rec = self._day_open.get(tick.symbol)
        if day_open_rec is None or day_open_rec[0] != kst_date:
            self._day_open[tick.symbol] = (kst_date, tick.price)
        day_open = self._day_open[tick.symbol][1]

        pos = self._open.get(tick.symbol)

        if pos is not None:
            if tick.price > pos.max_seen:
                pos.max_seen = tick.price

            # 1) SL — 최우선
            if pos.sl_at_be:
                sl_price = pos.avg_entry
            else:
                sl_price = int(pos.initial_entry * (1 - self.stop_loss_pct / 100))
            if tick.price <= sl_price:
                remaining = pos.qty_frac_in_pos
                sigs: list[Signal] = []
                if remaining > 1e-6:
                    sigs.append(self._sig(
                        tick, Side.SELL, conf=remaining, urgency="high",
                        note=f"SL @ {tick.price} sl={sl_price}",
                    ))
                self._record_review(pos, tick, exit_reason="SL",
                                    exit_note=f"SL @ {tick.price}")
                del self._open[tick.symbol]
                return sigs

            # 1.5) 상한가 즉시 청산 (전량) — vb=당일청산이라 lock 시 더 hold 무의미
            pb_for_limit = self._resolve_prev_bar(tick.symbol, kst_date)
            pc = pb_for_limit[2] if pb_for_limit else 0
            if pc and pc > 0 and tick.price >= pc * (1 + self.limit_up_pct / 100):
                remaining = pos.qty_frac_in_pos
                sigs = []
                if remaining > 1e-6:
                    sigs.append(self._sig(
                        tick, Side.SELL, conf=remaining,
                        note=f"limit_up @ {tick.price} (+{(tick.price/pc-1)*100:.1f}%)",
                    ))
                self._record_review(pos, tick, exit_reason="limit_up",
                                    exit_note=f"limit_up @ {tick.price} (prev_close={pc})")
                del self._open[tick.symbol]
                return sigs

            # 2) Timeout
            if tick.timestamp - pos.entry_time >= self.max_hold:
                remaining = pos.qty_frac_in_pos
                sigs = []
                if remaining > 1e-6:
                    sigs.append(self._sig(
                        tick, Side.SELL, conf=remaining,
                        note="timeout",
                    ))
                self._record_review(pos, tick, exit_reason="timeout",
                                    exit_note="hold timeout")
                del self._open[tick.symbol]
                return sigs

            # 3) TP 한 단계만
            if pos.sell_levels_hit < len(self.exit_plan):
                tp_offset, tp_frac = self.exit_plan[pos.sell_levels_hit]
                tp_price = int(pos.initial_entry * (1 + tp_offset))
                if tick.price >= tp_price:
                    take = min(tp_frac, pos.qty_frac_in_pos)
                    sigs = []
                    if take > 1e-6:
                        sigs.append(self._sig(
                            tick, Side.SELL, conf=take,
                            note=f"TP{pos.sell_levels_hit + 1} @ {tick.price}",
                        ))
                        pos.qty_frac_in_pos -= take
                        pos.cum_sold += take
                    pos.sell_levels_hit += 1
                    if pos.sell_levels_hit == 1:
                        pos.sl_at_be = True
                    self._record_review(
                        pos, tick,
                        exit_reason=f"TP{pos.sell_levels_hit}",
                        exit_note=f"TP{pos.sell_levels_hit} partial @ {tick.price}",
                    )
                    if pos.qty_frac_in_pos <= 1e-6:
                        del self._open[tick.symbol]
                    return sigs

            # 4) Trailing
            if pos.trail_armed and pos.qty_frac_in_pos > 1e-6:
                trail_stop = int(pos.max_seen * (1 - self.trailing_pct / 100))
                if tick.price <= trail_stop:
                    remaining = pos.qty_frac_in_pos
                    sigs = [self._sig(
                        tick, Side.SELL, conf=remaining,
                        note=f"trail @ {tick.price} max={pos.max_seen}",
                    )]
                    self._record_review(pos, tick, exit_reason="trail",
                                        exit_note=f"trail @ {tick.price}")
                    del self._open[tick.symbol]
                    return sigs

            # 5) Additional entry — SELL 안 발화한 경우만 + 한 단계
            if pos.buy_levels_hit < len(self.entry_plan):
                e_offset, e_frac = self.entry_plan[pos.buy_levels_hit]
                e_trigger = int(pos.initial_entry * (1 + e_offset))
                if tick.price >= e_trigger:
                    sigs = [self._sig(
                        tick, Side.BUY, conf=e_frac,
                        note=f"E{pos.buy_levels_hit + 1} @ {tick.price} frac={e_frac:.2f}",
                    )]
                    pos.entries.append((tick.price, tick.timestamp, e_frac))
                    pos.cum_bought += e_frac
                    pos.qty_frac_in_pos += e_frac
                    pos.buy_levels_hit += 1
                    pos.last_entry_time = tick.timestamp
                    total_paid = sum(p * f for p, _, f in pos.entries)
                    total_frac = sum(f for _, _, f in pos.entries)
                    if total_frac > 0:
                        pos.avg_entry = int(total_paid / total_frac)
                    if pos.buy_levels_hit == len(self.entry_plan):
                        pos.trail_armed = True
                    return sigs

            return []

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
        if self.volume_filter is not None and not self.volume_filter.passes(tick.symbol):
            return []
        if self.entry_gate is not None and not self.entry_gate.passes(tick.symbol, tick.timestamp):
            return []
        self._entered_today.add(day_key)

        _, e_frac = self.entry_plan[0]
        new_pos = _ScalePos(
            initial_entry=tick.price,
            avg_entry=tick.price,
            entry_time=tick.timestamp,
            last_entry_time=tick.timestamp,
            entries=[(tick.price, tick.timestamp, e_frac)],
            buy_levels_hit=1,
            cum_bought=e_frac,
            qty_frac_in_pos=e_frac,
            max_seen=tick.price,
            trail_armed=(len(self.entry_plan) == 1),
        )
        self._open[tick.symbol] = new_pos
        return [Signal(
            symbol=tick.symbol, side=Side.BUY, confidence=e_frac,
            strategy=self.name, timestamp=tick.timestamp,
            note=f"vb_scalein E1 trigger={trigger} k={self.k} frac={e_frac:.2f}",
        )]

    def _sig(
        self, tick: Tick, side: Side, *, conf: float, note: str,
        urgency: str = "normal",
    ) -> Signal:
        return Signal(
            symbol=tick.symbol, side=side,
            confidence=min(max(conf, 0.0), 1.0),
            urgency=urgency,  # type: ignore[arg-type]
            strategy=self.name, timestamp=tick.timestamp, note=note,
        )

    def _record_review(
        self, pos: _ScalePos, tick: Tick, *,
        exit_reason: str, exit_note: str,
    ) -> None:
        if self.review_log is None:
            return
        with contextlib.suppress(Exception):
            self.review_log.record(TradeReview(
                strategy=self.name, symbol=tick.symbol,
                entry_ts=pos.entry_time, entry_price=pos.avg_entry, qty=1,
                exit_ts=tick.timestamp, exit_price=tick.price,
                pnl_krw=tick.price - pos.avg_entry, exit_reason=exit_reason,
                entry_note=f"vb_scalein avg={pos.avg_entry} levels_buy={pos.buy_levels_hit}",
                exit_note=exit_note,
            ))

    def open_positions(self) -> dict[str, _ScalePos]:
        return dict(self._open)
