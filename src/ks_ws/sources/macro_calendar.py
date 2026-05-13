"""MacroCalendarSource — CPI / FOMC / NFP 등 매크로 발표 이벤트 24h 회피 가드.

fundamental_strategy.md §3 Pattern 5 (Time-of-Day x Fundamental) + Pattern 3
(Entry Veto) 의 매크로 캘린더 변종. 미국 발표 (CPI / FOMC / NFP) 직전 24h 또는
직후 N h 동안 국장 단타 entry 회피.

이유:
- 미국 CPI / FOMC 발표 직후 미장 변동성 → 다음 국장 갭/방향성 예측 어려움.
- 단타 (신고가매매 / 종가베팅 / 패턴 매매) 는 평균회귀나 추세 지속 가정 — 매크로
  발표는 그 가정 자체를 깨는 외생 충격.
- 만쥬 / 주덕 등 유튜브 단타 강의에서도 "FOMC 직전/직후 매매 X" 룰 자주 언급.

API:
- ``MacroEvent`` — name + scheduled_utc datetime + severity (high/medium/low).
- ``MacroCalendarSource`` — events 리스트 보관, 현재 시각이 (event - hours_before,
  event + hours_after) 내인지 판단. 활성 이벤트 list 반환.
- ``MacroCalendarGate`` — strategy 래퍼. 활성 이벤트 있을 때 BUY signal 차단,
  SELL (exits) 는 통과.

이벤트 데이터 소스:
- V1 = 사용자/운영자가 시작 시점에 list 명시. 추후 V2 = ICS feed (econoday /
  forexfactory) auto-fetch.
- 기본 default = 빈 list. paper_trade 운영자가 매주 업데이트.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ks_ws.domain import Bar, OrderBook, Side, Signal, Tick
from ks_ws.events import Event
from ks_ws.strategies.base import Strategy


@dataclass(frozen=True)
class MacroEvent:
    name: str  # "CPI" / "FOMC" / "NFP" / "PPI" / "PCE"
    scheduled_utc: datetime
    severity: str = "high"  # "high" / "medium" / "low"

    def __post_init__(self) -> None:
        if self.scheduled_utc.tzinfo is None:
            raise ValueError("scheduled_utc must be timezone-aware")
        if self.severity not in ("high", "medium", "low"):
            raise ValueError(f"unknown severity: {self.severity!r}")


class MacroCalendarSource:
    """매크로 이벤트 캘린더. 현재 활성 (within window) 이벤트 list 반환."""

    def __init__(
        self,
        events: Iterable[MacroEvent] | None = None,
        *,
        hours_before: float = 24.0,
        hours_after: float = 2.0,
        severities: tuple[str, ...] = ("high",),
    ) -> None:
        if hours_before < 0 or hours_after < 0:
            raise ValueError("hours must be non-negative")
        for s in severities:
            if s not in ("high", "medium", "low"):
                raise ValueError(f"unknown severity: {s!r}")
        self._events: list[MacroEvent] = list(events or [])
        self.hours_before = hours_before
        self.hours_after = hours_after
        self.severities = tuple(severities)

    def add(self, event: MacroEvent) -> None:
        self._events.append(event)

    def events(self) -> list[MacroEvent]:
        return list(self._events)

    def active(self, now_utc: datetime | None = None) -> list[MacroEvent]:
        """현재 시각이 [event - hours_before, event + hours_after] 내인 이벤트 list."""
        now = now_utc or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("now_utc must be timezone-aware")
        before = timedelta(hours=self.hours_before)
        after = timedelta(hours=self.hours_after)
        out = []
        for ev in self._events:
            if ev.severity not in self.severities:
                continue
            if ev.scheduled_utc - before <= now <= ev.scheduled_utc + after:
                out.append(ev)
        return out

    def is_active(self, now_utc: datetime | None = None) -> bool:
        return bool(self.active(now_utc))


CalendarProvider = Callable[[], MacroCalendarSource]


class MacroCalendarGate(Strategy):
    """전략 래퍼. ``calendar.is_active()`` 일 때 BUY signal 차단.
    SELL (exits) 는 항상 통과 — 보유 포지션은 이벤트 직전에도 청산 가능해야 함.
    """

    def __init__(
        self,
        inner: Strategy,
        *,
        calendar: MacroCalendarSource,
    ) -> None:
        self.inner = inner
        self.name = inner.name
        self.calendar = calendar

    def _filter(self, sigs: list[Signal], ts: datetime) -> list[Signal]:
        if not self.calendar.is_active(ts):
            return sigs
        return [s for s in sigs if s.side != Side.BUY]

    def on_bar(self, bar: Bar) -> list[Signal]:
        return self._filter(self.inner.on_bar(bar), bar.timestamp)

    def on_tick(self, tick: Tick) -> list[Signal]:
        return self._filter(self.inner.on_tick(tick), tick.timestamp)

    def on_orderbook(self, orderbook: OrderBook) -> list[Signal]:
        return self._filter(self.inner.on_orderbook(orderbook), orderbook.timestamp)

    def on_event(self, event: Event) -> list[Signal]:
        return self._filter(self.inner.on_event(event), event.timestamp)


def default_2026_q2_calendar() -> MacroCalendarSource:
    """V1 시드 — 2026 Q2 미국 매크로 발표 일정 (UTC).

    날짜는 실제 공식 일정 확인 후 운영자가 paper_trade 시작 시 갱신해야 함.
    아래는 *예시* — 매월 둘째 화요일 CPI / 둘째 금요일 PPI / 첫째 금요일 NFP /
    분기 셋째 수요일 FOMC.
    """
    seed = [
        # CPI (BLS, 월간) — KST = UTC+9, 미 동부 08:30 = UTC 12:30
        MacroEvent("CPI", datetime(2026, 4, 14, 12, 30, tzinfo=UTC)),
        MacroEvent("CPI", datetime(2026, 5, 12, 12, 30, tzinfo=UTC)),
        MacroEvent("CPI", datetime(2026, 6, 9, 12, 30, tzinfo=UTC)),
        # PPI (BLS, 월간) — 보통 CPI 다음날
        MacroEvent("PPI", datetime(2026, 4, 15, 12, 30, tzinfo=UTC), severity="medium"),
        MacroEvent("PPI", datetime(2026, 5, 13, 12, 30, tzinfo=UTC), severity="medium"),
        # FOMC (Fed, 분기 ~2회) — 미 동부 14:00 = UTC 18:00
        MacroEvent("FOMC", datetime(2026, 4, 29, 18, 0, tzinfo=UTC)),
        MacroEvent("FOMC", datetime(2026, 6, 17, 18, 0, tzinfo=UTC)),
        # NFP (BLS, 첫째 금요일)
        MacroEvent("NFP", datetime(2026, 4, 3, 12, 30, tzinfo=UTC)),
        MacroEvent("NFP", datetime(2026, 5, 1, 12, 30, tzinfo=UTC)),
        MacroEvent("NFP", datetime(2026, 6, 5, 12, 30, tzinfo=UTC)),
    ]
    return MacroCalendarSource(seed, hours_before=24.0, hours_after=2.0)
