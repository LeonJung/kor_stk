"""KRX (한국거래소) 영업일 calendar.

평일 09:00-15:30 KST 정규장 + 시간외 단일가 (15:30-18:00 / 16:00-18:00 등) 가
거래 시간. 본 모듈은 영업일 (full-day closure 가 아닌 날) 판정 + next/prev
trading day 헬퍼 + 정규장 시간 게이트를 제공.

휴장일 데이터는 정적 list (2025-2026). KRX 가 매년 12월 다음해 휴장일 발표 →
연 1회 갱신. 데이터 출처: calendarlabs.com (web 검증 2026-05-09).

향후 확장:
- 임시휴장 (천재지변, 시스템 장애 등) → 사용자 수동 추가 hook
- 2027+ 데이터 추가 (연 1회)
- 시간외 단일가 시간 (정규장과 별도)
"""

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")

# Regular session hours (KST)
_SESSION_OPEN = time(9, 0)
_SESSION_CLOSE = time(15, 30)

# Confirmed full-day market closures.
# Source: KRX official + calendarlabs.com (web 검증 2026-05-09).
_CLOSURES: frozenset[date] = frozenset(
    {
        # 2025
        date(2025, 1, 1),  # New Year
        date(2025, 1, 28),  # Lunar New Year eve
        date(2025, 1, 29),  # Lunar New Year
        date(2025, 1, 30),  # Lunar New Year holiday
        date(2025, 3, 1),  # Independence Movement (사실 토요일 — 데이터엔 폐장 아님)
        date(2025, 5, 1),  # Labor Day
        date(2025, 5, 5),  # Children's Day
        date(2025, 5, 6),  # Children's Day substitute
        date(2025, 6, 6),  # Memorial Day
        date(2025, 8, 15),  # Liberation Day
        date(2025, 10, 3),  # National Foundation
        date(2025, 10, 6),  # Chuseok
        date(2025, 10, 7),  # Chuseok
        date(2025, 10, 8),  # Chuseok holiday
        date(2025, 10, 9),  # Hangul Day
        date(2025, 12, 25),  # Christmas
        date(2025, 12, 31),  # Year-end early close (full closure 처리 — 보수적)
        # 2026 (calendarlabs.com 발표)
        date(2026, 1, 1),  # New Year
        date(2026, 2, 16),  # Lunar New Year
        date(2026, 2, 17),
        date(2026, 2, 18),
        date(2026, 3, 2),  # Independence Movement (3/1 일요일 → 월 대체)
        date(2026, 5, 1),  # Labor Day
        date(2026, 5, 5),  # Children's Day
        date(2026, 5, 25),  # Buddha's Birthday
        date(2026, 8, 17),  # Liberation Day (8/15 토 → 월 대체)
        date(2026, 9, 24),  # Chuseok
        date(2026, 9, 25),
        date(2026, 10, 5),  # National Foundation (10/3 토 → 월 대체)
        date(2026, 10, 9),  # Hangul Day
        date(2026, 12, 25),  # Christmas
    }
)


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # 5=Sat, 6=Sun


def is_market_holiday(d: date) -> bool:
    """True if d is in the static KRX closure list."""
    return d in _CLOSURES


def is_trading_day(d: date) -> bool:
    """True if KRX regular session runs on this date (not weekend/holiday)."""
    return not (is_weekend(d) or is_market_holiday(d))


def is_market_open(now: datetime | None = None) -> bool:
    """True if right now falls inside the KRX regular session window
    (Mon-Fri 09:00-15:30 KST, excluding holidays).
    """
    now = now or datetime.now(_KST)
    if now.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    local = now.astimezone(_KST)
    if not is_trading_day(local.date()):
        return False
    return _SESSION_OPEN <= local.time() < _SESSION_CLOSE


def next_trading_day(d: date) -> date:
    """Smallest trading day strictly after d (skips weekends + holidays).
    Bounded search up to 365 days to surface bad calendar data."""
    cur = d + timedelta(days=1)
    for _ in range(365):
        if is_trading_day(cur):
            return cur
        cur += timedelta(days=1)
    raise RuntimeError(f"no trading day found within 365 days of {d}")


def prev_trading_day(d: date) -> date:
    """Largest trading day strictly before d."""
    cur = d - timedelta(days=1)
    for _ in range(365):
        if is_trading_day(cur):
            return cur
        cur -= timedelta(days=1)
    raise RuntimeError(f"no trading day found within 365 days before {d}")


def trading_days_between(start: date, end: date) -> list[date]:
    """Inclusive trading days in [start, end]. Empty if start > end."""
    if start > end:
        return []
    out: list[date] = []
    cur = start
    while cur <= end:
        if is_trading_day(cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out


def add_holidays(holidays: Iterable[date]) -> None:
    """Add ad-hoc closures (e.g., emergency suspensions) at runtime.
    Extends the in-process set; not persisted."""
    global _CLOSURES
    _CLOSURES = frozenset(_CLOSURES | set(holidays))


def session_hours_kst() -> tuple[time, time]:
    """Return (open, close) times for the regular KRX session in KST."""
    return _SESSION_OPEN, _SESSION_CLOSE
