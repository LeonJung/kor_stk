from datetime import UTC, datetime, timedelta, timezone

from ks_ws.bus import EventBus
from ks_ws.domain import Tick
from ks_ws.market.hub import Tier
from ks_ws.market.kis_hub import KisMarketDataHub, parse_trade_record


def _trade_record(symbol="005930", time_str="103045", price=70_000, volume=10) -> list[str]:
    """Build a 13-field H0STCNT0 record. Padded with empty strings for unused fields."""
    rec = [""] * 13
    rec[0] = symbol
    rec[1] = time_str
    rec[2] = str(price)
    rec[12] = str(volume)
    return rec


def test_parse_trade_record_basic_fields():
    rec = _trade_record(symbol="005930", time_str="103045", price=70_500, volume=15)
    tick = parse_trade_record(rec)
    assert tick is not None
    assert tick.symbol == "005930"
    assert tick.price == 70_500
    assert tick.volume == 15


def test_parse_trade_record_returns_utc_timestamp():
    rec = _trade_record(time_str="103045")
    tick = parse_trade_record(rec)
    assert tick is not None
    assert tick.timestamp.tzinfo == UTC
    # 10:30:45 KST == 01:30:45 UTC (KST is +9)
    expected_utc_time = datetime.now(UTC).replace(hour=1, minute=30, second=45, microsecond=0)
    # The date should match today's KST date converted to UTC
    kst = timezone(timedelta(hours=9))
    today_kst = datetime.now(kst).date()
    expected_kst = datetime(today_kst.year, today_kst.month, today_kst.day, 10, 30, 45, tzinfo=kst)
    assert tick.timestamp == expected_kst.astimezone(UTC)
    # Sanity: same time of day modulo date differences
    assert tick.timestamp.hour == expected_utc_time.hour
    assert tick.timestamp.minute == expected_utc_time.minute


def test_parse_trade_record_short_record_returns_none():
    rec = ["005930", "103045", "70000"]  # only 3 fields, need 13
    assert parse_trade_record(rec) is None


def test_parse_trade_record_invalid_int_returns_none():
    rec = _trade_record()
    rec[2] = "not-a-number"
    assert parse_trade_record(rec) is None


def test_hub_assign_only_hot_for_now():
    """WARM/COLD assignments are recorded but the v1 hub doesn't act on them."""
    bus = EventBus()
    hub = KisMarketDataHub(bus)
    hub.assign("005930", Tier.HOT)
    hub.assign("000660", Tier.WARM)
    hub.assign("035420", Tier.COLD)
    assert hub.symbols_by_tier(Tier.HOT) == ["005930"]
    assert hub.symbols_by_tier(Tier.WARM) == ["000660"]
    assert hub.symbols_by_tier(Tier.COLD) == ["035420"]


def test_handle_frame_publishes_tick_to_bus():
    """Inject a fake H0STCNT0 frame and verify a Tick lands on the bus.
    This bypasses the WS connection entirely — frame parsing is what we test."""
    bus = EventBus()
    sub = bus.subscribe(Tick)
    hub = KisMarketDataHub(bus)

    # Stand in for self._feed without actually opening WS.
    class _FakeFeed:
        @staticmethod
        def parse_frame(raw):
            return KisMarketDataHub.__bases__[0]  # placeholder, see below

    # Use the real parser instead — it's a static method.
    from ks_ws.kis.realtime import KisRealtimeFeed

    class _Stub:
        parse_frame = staticmethod(KisRealtimeFeed.parse_frame)

    hub._feed = _Stub()  # type: ignore[assignment]

    # Build a frame: 0|H0STCNT0|001|<13 fields joined by ^>
    fields = [""] * 13
    fields[0] = "005930"
    fields[1] = "103045"
    fields[2] = "70000"
    fields[12] = "10"
    frame = f"0|H0STCNT0|001|{'^'.join(fields)}"

    hub._handle_frame(frame)
    assert sub.qsize() == 1
    tick = sub.get_nowait()
    assert isinstance(tick, Tick)
    assert tick.symbol == "005930"
    assert tick.price == 70_000
    assert tick.volume == 10


def test_handle_frame_ignores_unknown_tr_id():
    bus = EventBus()
    sub = bus.subscribe(Tick)
    hub = KisMarketDataHub(bus)

    from ks_ws.kis.realtime import KisRealtimeFeed

    class _Stub:
        parse_frame = staticmethod(KisRealtimeFeed.parse_frame)

    hub._feed = _Stub()  # type: ignore[assignment]

    fields = [""] * 13
    fields[0] = "005930"
    fields[1] = "103045"
    fields[2] = "70000"
    fields[12] = "10"
    frame = f"0|H0STASP0|001|{'^'.join(fields)}"  # 호가, not 체결

    hub._handle_frame(frame)
    assert sub.qsize() == 0


def test_handle_frame_skips_malformed_records():
    bus = EventBus()
    sub = bus.subscribe(Tick)
    hub = KisMarketDataHub(bus)

    from ks_ws.kis.realtime import KisRealtimeFeed

    class _Stub:
        parse_frame = staticmethod(KisRealtimeFeed.parse_frame)

    hub._feed = _Stub()  # type: ignore[assignment]

    # Two records: the first malformed (only 3 fields), the second valid.
    good = [""] * 13
    good[0] = "005930"
    good[1] = "103045"
    good[2] = "70000"
    good[12] = "10"
    bad = ["005930", "103045", "70000"] + [""] * 10
    bad[2] = "not-a-number"  # int parse fails
    # KIS uses count=2, fields are concatenated
    payload = "^".join(good + bad)
    frame = f"0|H0STCNT0|002|{payload}"

    hub._handle_frame(frame)
    assert sub.qsize() == 1  # only the good one
