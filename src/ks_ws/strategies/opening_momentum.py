"""OpeningMomentumStrategy — 09:03-09:25 (KST) 시장 개장 직후 모멘텀 매매.

사용자 인사이트 (technical_strategy.md G): 진짜 수익 잘 나는 종목은 09:00 시초 시점에는
별 변동 없다가 **09:03~09:25** 사이 갑자기 쭉 치고 올리는 패턴. 거래대금
상위 종목을 watchlist 로 두고 시초가 대비 +5% 이상 급등 + 거래량 spike 시
진입. 09:50 강제 청산, 매수가 정확 hit 시 즉시 손절.

거래대금 상위 진짜 의미: **전날 종가베팅 보유자 매도 ↔ 신규 매수자 받음의
교체**. 단순 거래대금 상위 ≠ 매수 신호. 09:03-09:25 강한 급등 동반 시만
모멘텀 신호.

시간 윈도우는 strategy 자체가 관리 — TimeWindowGate 외부 wrap 은 시초가
capture 까지 차단해버려 09:03 이전의 첫 tick 으로 open price 를 잡지 못한다.
대신 ``entry_window_kst`` 파라미터로 entry 만 시간 제약하고, open price
capture / exit 관리는 항상 수행한다. force_exit_kst 가 09:50 강제 청산.
"""

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ks_ws.domain import Side, Signal, Tick
from ks_ws.storage.trade_review import TradeReview, TradeReviewLog
from ks_ws.strategies.base import Strategy

_KST = ZoneInfo("Asia/Seoul")


@dataclass
class _Position:
    symbol: str
    entry_price: int
    entry_time: datetime
    open_price: int  # 시초가 reference
    tp_price: int | None = None
    sl_price: int | None = None


@dataclass
class _SymbolMeta:
    open_price: int
    open_time: datetime  # 09:00 시각의 first tick


class OpeningMomentumStrategy(Strategy):
    name = "opening_momentum"
    style = "scalping"  # 사용자 룰 (2026-05-15) — 스캘핑 ≤15min

    def __init__(
        self,
        *,
        watchlist: set[str],
        surge_pct: float = 10.0,  # 사용자 룰 (2026-05-15) 5→10 강화
        take_profit_pct: float = 1.5,
        stop_loss_pct: float = 0.8,
        entry_window_kst: tuple[time, time] = (time(9, 3), time(9, 25)),
        force_exit_kst: time = time(9, 25),  # 사용자 룰 (2026-05-15) 9:50→9:25
        volume_spike_multiplier: float = 3.0,  # 사용자 룰 (2026-05-15) 거래량 ×3 필터
        volume_lookback_minutes: int = 5,
        confidence: float = 0.6,
        review_log: TradeReviewLog | None = None,
        atr_provider=None,
    ) -> None:
        if not watchlist:
            raise ValueError("watchlist must not be empty")
        if surge_pct <= 0 or take_profit_pct <= 0 or stop_loss_pct <= 0:
            raise ValueError("surge_pct, take_profit_pct, stop_loss_pct must be positive")
        if not 0 < confidence <= 1:
            raise ValueError("confidence must be in (0, 1]")
        if entry_window_kst[0] >= entry_window_kst[1]:
            raise ValueError("entry_window start must be < end")
        if volume_spike_multiplier < 1:
            raise ValueError("volume_spike_multiplier must be >= 1")
        self.watchlist = set(watchlist)
        self.surge_pct = surge_pct
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.entry_window_kst = entry_window_kst
        self.force_exit_kst = force_exit_kst
        self.volume_spike_multiplier = volume_spike_multiplier
        self.volume_lookback_minutes = volume_lookback_minutes
        self.confidence = confidence
        self.review_log = review_log
        self.atr_provider = atr_provider
        self._meta: dict[str, _SymbolMeta] = {}  # opening price snapshot per symbol
        self._open: dict[str, _Position] = {}  # current positions
        # symbol → list[(timestamp, volume)] 최근 N분 거래량 추적
        self._recent_volumes: dict[str, list[tuple[datetime, int]]] = {}
        self._baseline_volume: dict[str, float] = {}

    def _record_review(self, pos: "_Position", tick: Tick, *, exit_reason: str,
                       exit_note: str) -> None:
        if self.review_log is None:
            return
        with contextlib.suppress(Exception):
            self.review_log.record(TradeReview(
                strategy=self.name, symbol=tick.symbol,
                entry_ts=pos.entry_time, entry_price=pos.entry_price, qty=1,
                exit_ts=tick.timestamp, exit_price=tick.price,
                pnl_krw=tick.price - pos.entry_price,
                exit_reason=exit_reason,
                entry_note=f"opening_mom: open={pos.open_price} entry={pos.entry_price}",
                exit_note=exit_note,
            ))

    def on_tick(self, tick: Tick) -> list[Signal]:
        if tick.symbol not in self.watchlist:
            return []
        # Capture opening price on the first tick we see for this symbol today
        if tick.symbol not in self._meta:
            self._meta[tick.symbol] = _SymbolMeta(
                open_price=tick.price, open_time=tick.timestamp
            )
        # 최근 N분 거래량 추적 (시초 5분 평균 대비 spike 확인용)
        vols = self._recent_volumes.setdefault(tick.symbol, [])
        vols.append((tick.timestamp, tick.volume))
        cutoff = tick.timestamp - timedelta(minutes=self.volume_lookback_minutes * 2)
        # 오래된 entries 제거
        while vols and vols[0][0] < cutoff:
            vols.pop(0)

        # Position management (exit) ahead of entry
        pos = self._open.get(tick.symbol)
        if pos is not None:
            return self._maybe_exit(tick, pos)

        # Entry only inside entry_window (KST)
        local_t = tick.timestamp.astimezone(_KST).time()
        if not (self.entry_window_kst[0] <= local_t < self.entry_window_kst[1]):
            return []

        # Entry: surge from open price
        meta = self._meta[tick.symbol]
        if meta.open_price <= 0:
            return []
        surge_pct = (tick.price - meta.open_price) / meta.open_price * 100
        if surge_pct < self.surge_pct:
            return []

        # 거래량 spike 필터 (사용자 룰 2026-05-15)
        # 최근 N분 거래량 합 ≥ 직전 N분 거래량 합 × multiplier
        recent_window_start = tick.timestamp - timedelta(minutes=self.volume_lookback_minutes)
        recent_vol = sum(v for ts, v in vols if ts >= recent_window_start)
        prior_vol = sum(v for ts, v in vols if ts < recent_window_start)
        if prior_vol > 0 and recent_vol < prior_vol * self.volume_spike_multiplier:
            return []
        # Bootstrap: 첫 entries (prior_vol=0) 시 한 번 통과 허용
        # Open a position; record entry price = current tick price
        from ks_ws.strategies._atr_helper import resolve_tp_sl
        tp_price, sl_price = resolve_tp_sl(
            tick.price, tick.symbol,
            atr_provider=self.atr_provider, style=self.style,
            fallback_tp_pct=self.take_profit_pct,
            fallback_sl_pct=self.stop_loss_pct,
        )
        self._open[tick.symbol] = _Position(
            symbol=tick.symbol,
            entry_price=tick.price,
            entry_time=tick.timestamp,
            open_price=meta.open_price,
            tp_price=tp_price,
            sl_price=sl_price,
        )
        return [
            Signal(
                symbol=tick.symbol,
                side=Side.BUY,
                confidence=self.confidence,
                strategy=self.name,
                timestamp=tick.timestamp,
                note=f"opening surge +{surge_pct:.1f}% from {meta.open_price} → {tick.price}",
            )
        ]

    def _maybe_exit(self, tick: Tick, pos: _Position) -> list[Signal]:
        # Take-profit (ATR-based if available)
        tp_price = pos.tp_price if pos.tp_price is not None else pos.entry_price * (1 + self.take_profit_pct / 100)
        if tick.price >= tp_price:
            del self._open[tick.symbol]
            self._record_review(pos, tick, exit_reason="TP",
                                exit_note=f"TP @ {tick.price}")
            return [self._exit(tick, note=f"take-profit @ {tick.price}")]
        # Stop-loss = entry price exact hit (사용자 B-6 결정).
        # ATR SL 도 같이 검사 — 더 보수적 (높은 가격) 인 쪽으로 SELL 자동.
        sl_atr = pos.sl_price if pos.sl_price is not None else pos.entry_price * (1 - self.stop_loss_pct / 100)
        sl = max(pos.entry_price, sl_atr)
        if tick.price <= sl:
            del self._open[tick.symbol]
            self._record_review(pos, tick, exit_reason="SL",
                                exit_note=f"entry hit @ {tick.price}")
            return [self._exit(tick, note=f"entry hit @ {tick.price}", urgency="high")]
        # Time-based force exit at 09:50 KST
        # NOTE: 다른 strategy 들은 no_force_close 룰 따르지만 시초 모멘텀은 책
        # strategy G 의 핵심 룰 (단타 = 시간 정해놓고 청산). 예외 적용.
        local_t = tick.timestamp.astimezone(_KST).time()
        if local_t >= self.force_exit_kst:
            del self._open[tick.symbol]
            self._record_review(pos, tick, exit_reason="timeout",
                                exit_note=f"force exit @ {self.force_exit_kst}")
            return [self._exit(tick, note=f"force exit @ {self.force_exit_kst} KST")]
        return []

    def _exit(self, tick: Tick, *, note: str, urgency: str = "normal") -> Signal:
        return Signal(
            symbol=tick.symbol,
            side=Side.SELL,
            confidence=1.0,
            urgency=urgency,  # type: ignore[arg-type]
            strategy=self.name,
            timestamp=tick.timestamp,
            note=note,
        )

    def reset_for_new_session(self) -> None:
        """Call at start of each trading day to clear opening snapshots and
        any stranded positions (positions should normally be closed by 09:50)."""
        self._meta.clear()
        self._open.clear()

    def open_positions(self) -> dict[str, _Position]:
        return dict(self._open)


def kst_dt(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> datetime:
    """Helper for tests / configs — build a UTC datetime from KST wall clock."""
    return datetime(year, month, day, hour, minute, second, tzinfo=_KST).astimezone(UTC)
