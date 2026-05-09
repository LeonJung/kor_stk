"""Tests for KRX calendar."""

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from ks_ws.calendar import (
    add_holidays,
    is_market_holiday,
    is_market_open,
    is_trading_day,
    is_weekend,
    next_trading_day,
    prev_trading_day,
    session_hours_kst,
    trading_days_between,
)

_KST = ZoneInfo("Asia/Seoul")


def _kst(year, month, day, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=_KST)


# is_weekend / is_market_holiday / is_trading_day -------------------------


def test_weekend_recognized():
    assert is_weekend(date(2026, 5, 9))  # Sat
    assert is_weekend(date(2026, 5, 10))  # Sun
    assert not is_weekend(date(2026, 5, 11))  # Mon


def test_known_holidays_2026():
    assert is_market_holiday(date(2026, 1, 1))
    assert is_market_holiday(date(2026, 5, 5))  # Children's Day
    assert is_market_holiday(date(2026, 9, 24))  # Chuseok
    assert is_market_holiday(date(2026, 10, 9))  # Hangul
    assert is_market_holiday(date(2026, 12, 25))  # Christmas


def test_known_holidays_2025():
    assert is_market_holiday(date(2025, 1, 1))
    assert is_market_holiday(date(2025, 5, 5))
    assert is_market_holiday(date(2025, 10, 9))


def test_trading_day_excludes_weekend_and_holiday():
    assert is_trading_day(date(2026, 5, 11))  # Mon, normal
    assert not is_trading_day(date(2026, 5, 9))  # Sat
    assert not is_trading_day(date(2026, 5, 5))  # Children's Day (Tue)


# is_market_open ----------------------------------------------------------


def test_market_open_during_session():
    assert is_market_open(_kst(2026, 5, 11, 10, 0))


def test_market_closed_before_session():
    assert not is_market_open(_kst(2026, 5, 11, 8, 59))


def test_market_closed_at_session_close():
    assert not is_market_open(_kst(2026, 5, 11, 15, 30))  # exclusive


def test_market_closed_on_weekend():
    assert not is_market_open(_kst(2026, 5, 9, 10, 0))  # Saturday


def test_market_closed_on_holiday():
    assert not is_market_open(_kst(2026, 5, 5, 10, 0))  # Children's Day


def test_market_open_handles_utc_input():
    """Pass UTC time and verify it's converted to KST for comparison."""
    utc_morning = datetime(2026, 5, 11, 1, 0, tzinfo=UTC)  # 10:00 KST
    assert is_market_open(utc_morning)


def test_market_open_rejects_naive_datetime():
    with pytest.raises(ValueError, match="timezone-aware"):
        is_market_open(datetime(2026, 5, 11, 10, 0))


# next/prev_trading_day ---------------------------------------------------


def test_next_trading_day_skips_weekend():
    # Friday 2026-05-08 → next is Monday 2026-05-11
    assert next_trading_day(date(2026, 5, 8)) == date(2026, 5, 11)


def test_next_trading_day_skips_holiday():
    # 2026-05-04 (Mon) → next is Wed 2026-05-06 (Tue is Children's Day)
    assert next_trading_day(date(2026, 5, 4)) == date(2026, 5, 6)


def test_next_trading_day_skips_long_holiday():
    # Lunar New Year 2026: 2/16 Mon, 2/17 Tue, 2/18 Wed all closed
    # 2/13 Fri → next is 2/19 Thu
    assert next_trading_day(date(2026, 2, 13)) == date(2026, 2, 19)


def test_prev_trading_day_skips_weekend():
    # Monday 2026-05-11 → prev is Friday 2026-05-08
    assert prev_trading_day(date(2026, 5, 11)) == date(2026, 5, 8)


def test_prev_trading_day_skips_holiday():
    # 2026-05-06 (Wed) → prev is Mon 2026-05-04 (Tue is Children's Day)
    assert prev_trading_day(date(2026, 5, 6)) == date(2026, 5, 4)


# trading_days_between ----------------------------------------------------


def test_trading_days_between_basic():
    # Mon 5/11 ~ Fri 5/15 = 5 trading days
    days = trading_days_between(date(2026, 5, 11), date(2026, 5, 15))
    assert len(days) == 5
    assert days[0] == date(2026, 5, 11)
    assert days[-1] == date(2026, 5, 15)


def test_trading_days_between_includes_holiday_skip():
    # 5/4 Mon ~ 5/8 Fri, but 5/5 Tue is Children's Day → 4 trading days
    days = trading_days_between(date(2026, 5, 4), date(2026, 5, 8))
    assert len(days) == 4
    assert date(2026, 5, 5) not in days


def test_trading_days_between_inverted_returns_empty():
    assert trading_days_between(date(2026, 5, 15), date(2026, 5, 11)) == []


# add_holidays ------------------------------------------------------------


def test_add_holidays_runtime():
    custom = date(2030, 7, 4)
    assert is_trading_day(custom)  # initially yes
    add_holidays([custom])
    assert is_market_holiday(custom)
    assert not is_trading_day(custom)


# session_hours -----------------------------------------------------------


def test_session_hours_default():
    open_t, close_t = session_hours_kst()
    assert open_t == time(9, 0)
    assert close_t == time(15, 30)
