"""OpeningMomentumStrategy — 09:03-09:25 (KST) 시장 개장 직후 모멘텀 매매.

사용자 인사이트 (strategy.md G): 진짜 수익 잘 나는 종목은 09:00 시초 시점에는
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

from dataclasses import dataclass
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from ks_ws.domain import Side, Signal, Tick
from ks_ws.strategies.base import Strategy

_KST = ZoneInfo("Asia/Seoul")


@dataclass
class _Position:
    symbol: str
    entry_price: int
    entry_time: datetime
    open_price: int  # 시초가 reference


@dataclass
class _SymbolMeta:
    open_price: int
    open_time: datetime  # 09:00 시각의 first tick


class OpeningMomentumStrategy(Strategy):
    name = "opening_momentum"

    def __init__(
        self,
        *,
        watchlist: set[str],
        surge_pct: float = 5.0,
        take_profit_pct: float = 3.0,
        entry_window_kst: tuple[time, time] = (time(9, 3), time(9, 25)),
        force_exit_kst: time = time(9, 50),
        confidence: float = 0.6,
    ) -> None:
        if not watchlist:
            raise ValueError("watchlist must not be empty")
        if surge_pct <= 0 or take_profit_pct <= 0:
            raise ValueError("surge_pct and take_profit_pct must be positive")
        if not 0 < confidence <= 1:
            raise ValueError("confidence must be in (0, 1]")
        if entry_window_kst[0] >= entry_window_kst[1]:
            raise ValueError("entry_window start must be < end")
        self.watchlist = set(watchlist)
        self.surge_pct = surge_pct
        self.take_profit_pct = take_profit_pct
        self.entry_window_kst = entry_window_kst
        self.force_exit_kst = force_exit_kst
        self.confidence = confidence
        self._meta: dict[str, _SymbolMeta] = {}  # opening price snapshot per symbol
        self._open: dict[str, _Position] = {}  # current positions

    def on_tick(self, tick: Tick) -> list[Signal]:
        if tick.symbol not in self.watchlist:
            return []
        # Capture opening price on the first tick we see for this symbol today
        if tick.symbol not in self._meta:
            self._meta[tick.symbol] = _SymbolMeta(
                open_price=tick.price, open_time=tick.timestamp
            )

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
        # Open a position; record entry price = current tick price
        self._open[tick.symbol] = _Position(
            symbol=tick.symbol,
            entry_price=tick.price,
            entry_time=tick.timestamp,
            open_price=meta.open_price,
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
        # Take-profit
        tp_price = pos.entry_price * (1 + self.take_profit_pct / 100)
        if tick.price >= tp_price:
            del self._open[tick.symbol]
            return [self._exit(tick, note=f"take-profit @ {tick.price}")]
        # Stop-loss = entry price exact hit (사용자 B-6 결정)
        if tick.price <= pos.entry_price:
            del self._open[tick.symbol]
            return [self._exit(tick, note=f"entry hit @ {tick.price}", urgency="high")]
        # Time-based force exit at 09:50 KST
        local_t = tick.timestamp.astimezone(_KST).time()
        if local_t >= self.force_exit_kst:
            del self._open[tick.symbol]
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
