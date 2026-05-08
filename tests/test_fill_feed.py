import json

from ks_ws.domain import Side
from ks_ws.kis.realtime import KisRealtimeFeed
from ks_ws.sources.fill_feed import FillEvent, KisFillFeed, parse_fill_record


def _fill_record(
    *,
    order_id="0000123456",
    side="02",  # 02 = buy
    symbol="005930",
    qty=10,
    price=70_000,
    time_str="103045",
    rejected="0",
) -> list[str]:
    rec = [""] * 13
    rec[0] = "USERHTSID"
    rec[1] = "50186679-01"
    rec[2] = order_id
    rec[3] = order_id  # original order
    rec[4] = side
    rec[5] = "0"
    rec[6] = "00"
    rec[7] = ""
    rec[8] = symbol
    rec[9] = str(qty)
    rec[10] = str(price)
    rec[11] = time_str
    rec[12] = rejected
    return rec


def test_parse_fill_record_buy():
    rec = _fill_record(side="02")
    e = parse_fill_record(rec)
    assert e is not None
    assert e.side == Side.BUY
    assert e.symbol == "005930"
    assert e.quantity == 10
    assert e.price == 70_000
    assert e.order_id == "0000123456"


def test_parse_fill_record_sell():
    rec = _fill_record(side="01")
    e = parse_fill_record(rec)
    assert e is not None
    assert e.side == Side.SELL


def test_parse_fill_record_rejected_returns_none():
    rec = _fill_record(rejected="1")
    assert parse_fill_record(rec) is None


def test_parse_fill_record_short_returns_none():
    assert parse_fill_record(["a", "b"]) is None


def test_parse_fill_record_zero_quantity_returns_none():
    rec = _fill_record(qty=0)
    assert parse_fill_record(rec) is None


def test_parse_fill_record_invalid_int_returns_none():
    rec = _fill_record(qty=10)
    rec[10] = "not-a-number"
    assert parse_fill_record(rec) is None


def test_handle_frame_dispatches_to_callback(monkeypatch):
    """Plain (unencrypted) H0STCNI9 frame should produce a FillEvent."""
    received: list[FillEvent] = []

    def cb(event: FillEvent) -> None:
        received.append(event)

    feed = KisRealtimeFeed()
    monkeypatch.setattr(feed._settings, "hts_id", "USERHTSID")
    fill_feed = KisFillFeed(feed, cb)

    rec = _fill_record()
    # Build plain (enc=0) frame — fill_feed will skip decryption
    frame = f"0|{fill_feed.tr_id}|001|{'^'.join(rec)}"
    fill_feed.handle_frame(frame)
    assert len(received) == 1
    assert received[0].order_id == "0000123456"
    assert fill_feed.received == 1
    assert fill_feed.parsed == 1


def test_handle_frame_decrypts_encrypted_payload(monkeypatch):
    """Encrypted frame: realtime feed should have AES keys cached, the
    fill feed decrypts and parses."""
    import base64

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    received: list[FillEvent] = []
    feed = KisRealtimeFeed()
    monkeypatch.setattr(feed._settings, "hts_id", "USERHTSID")
    fill_feed = KisFillFeed(feed, received.append)

    # Pre-stuff AES keys (skip the JSON-ack capture path)
    key = b"0123456789abcdef0123456789abcdef"
    iv = b"fedcba9876543210"
    feed._aes_keys[fill_feed.tr_id] = (key.decode(), iv.decode())

    rec = _fill_record()
    plain = "^".join(rec)
    plain_b = plain.encode("utf-8")
    pad_len = 16 - (len(plain_b) % 16)
    padded = plain_b + bytes([pad_len]) * pad_len
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct_b64 = base64.b64encode(enc.update(padded) + enc.finalize()).decode("ascii")

    # Encrypted frame — enc flag = 1, payload is the base64 ciphertext
    frame = f"1|{fill_feed.tr_id}|001|{ct_b64}"
    fill_feed.handle_frame(frame)
    assert len(received) == 1
    assert received[0].order_id == "0000123456"


def test_aes_keys_captured_from_subscription_ack():
    """When the realtime feed yields an ack JSON containing key+iv, the
    feed should cache them."""
    feed = KisRealtimeFeed()
    ack = json.dumps(
        {
            "header": {"tr_id": "H0STCNI9", "tr_key": "USERHTSID", "encrypt": "Y"},
            "body": {
                "rt_cd": "0",
                "msg1": "subscribe success",
                "output": {"key": "K" * 32, "iv": "I" * 16},
            },
        }
    )
    feed._maybe_capture_aes_keys(ack)
    assert "H0STCNI9" in feed._aes_keys
    assert feed.has_aes_keys("H0STCNI9")


def test_subscribe_without_hts_id_raises():
    import asyncio

    import pytest

    feed = KisRealtimeFeed()

    class _S:
        env = "mock"
        hts_id = ""

    fill_feed = KisFillFeed(feed, lambda _e: None)
    fill_feed._settings = _S()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="HTS_ID"):
        asyncio.run(fill_feed.subscribe())


def test_callback_exception_does_not_crash_handler():
    """A buggy callback shouldn't take down the dispatch loop."""

    def bad_cb(_event: FillEvent) -> None:
        raise RuntimeError("bug in callback")

    feed = KisRealtimeFeed()
    fill_feed = KisFillFeed(feed, bad_cb)
    rec = _fill_record()
    frame = f"0|{fill_feed.tr_id}|001|{'^'.join(rec)}"
    fill_feed.handle_frame(frame)  # must not raise
    # parsed counter still increments even if cb fails
    assert fill_feed.parsed == 1
