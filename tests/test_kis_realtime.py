import json

import httpx
import pytest

from ks_ws.kis import realtime as realtime_mod
from ks_ws.kis.realtime import (
    KisRealtimeFeed,
    build_subscribe_message,
    fetch_approval_key,
    subscribe_msg_for_orderbook,
    subscribe_msg_for_trade,
)


def test_build_subscribe_message_register():
    msg = build_subscribe_message("KEY1", "H0STCNT0", "005930", register=True)
    parsed = json.loads(msg)
    assert parsed["header"]["approval_key"] == "KEY1"
    assert parsed["header"]["tr_type"] == "1"
    assert parsed["header"]["custtype"] == "P"
    assert parsed["body"]["input"]["tr_id"] == "H0STCNT0"
    assert parsed["body"]["input"]["tr_key"] == "005930"


def test_build_subscribe_message_unregister():
    msg = build_subscribe_message("KEY1", "H0STCNT0", "005930", register=False)
    assert json.loads(msg)["header"]["tr_type"] == "2"


def test_trade_helper_uses_h0stcnt0():
    parsed = json.loads(subscribe_msg_for_trade("KEY", "005930"))
    assert parsed["body"]["input"]["tr_id"] == "H0STCNT0"


def test_orderbook_helper_uses_h0stasp0():
    parsed = json.loads(subscribe_msg_for_orderbook("KEY", "005930"))
    assert parsed["body"]["input"]["tr_id"] == "H0STASP0"


def test_fetch_approval_key_uses_secretkey_field(monkeypatch):
    """KIS quirk: approval endpoint expects `secretkey`, not `appsecret`."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/Approval"):
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"approval_key": "approved-123"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real_client_cls = httpx.Client

    def fake_client_cls(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr(realtime_mod.httpx, "Client", fake_client_cls)
    key = fetch_approval_key()
    assert key == "approved-123"
    assert "secretkey" in captured["body"]
    assert captured["body"]["grant_type"] == "client_credentials"


def test_parse_frame_single_record():
    """Standard data frame with one record."""
    frame = "0|H0STCNT0|001|005930^090000^70000^2"
    tr_id, enc, records = KisRealtimeFeed.parse_frame(frame)
    assert tr_id == "H0STCNT0"
    assert enc == "0"
    assert records == [["005930", "090000", "70000", "2"]]


def test_parse_frame_multiple_records():
    """Two records, 3 fields each → split evenly."""
    frame = "0|H0STCNT0|002|005930^090000^70000^005930^090001^70010"
    tr_id, _, records = KisRealtimeFeed.parse_frame(frame)
    assert tr_id == "H0STCNT0"
    assert records == [
        ["005930", "090000", "70000"],
        ["005930", "090001", "70010"],
    ]


def test_parse_frame_json_control():
    """Subscription ack / pingpong arrives as JSON, not pipe-delimited."""
    frame = '{"header":{"tr_id":"PINGPONG"},"body":{"output":{}}}'
    tr_id, _enc, records = KisRealtimeFeed.parse_frame(frame)
    assert tr_id == ""
    assert records == [[frame]]


def test_parse_frame_malformed_returns_raw():
    frame = "this-is-not-pipe-delimited"
    _tr_id, _enc, records = KisRealtimeFeed.parse_frame(frame)
    assert records == [[frame]]


def test_subscribe_without_connect_raises():
    feed = KisRealtimeFeed()
    feed._approval_key = "FAKE"  # avoid HTTP call
    with pytest.raises(RuntimeError, match="not connected"):
        import asyncio

        asyncio.run(feed.subscribe("H0STCNT0", "005930"))
