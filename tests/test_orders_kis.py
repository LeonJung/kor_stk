from datetime import UTC, datetime

import httpx
import pytest

from ks_ws import orders as orders_mod
from ks_ws.auth import token as token_mod
from ks_ws.config import get_settings
from ks_ws.domain import OrderIntent, Side
from ks_ws.orders import _ORDER_TR_IDS, KisOrderRejected, KisOrderRouter


def _ok_token():
    return {
        "access_token": "test_token",
        "token_type": "Bearer",
        "expires_in": 86400,
        "access_token_token_expired": "2099-01-01 00:00:00",
    }


def _setup(monkeypatch, tmp_path, *, order_response: dict, hashkey_response: dict | None = None):
    seen_requests = []
    if hashkey_response is None:
        hashkey_response = {"HASH": "test-hash-abc"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(200, json=_ok_token())
        if request.url.path.endswith("/uapi/hashkey"):
            return httpx.Response(200, json=hashkey_response)
        if request.url.path.endswith("/order-cash"):
            return httpx.Response(200, json=order_response)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    def fake_make_client(settings, **_kw):
        return httpx.Client(
            transport=transport,
            base_url="https://mock",
            headers={"appkey": settings.app_key, "appsecret": settings.app_secret},
        )

    monkeypatch.setattr(orders_mod, "make_client", fake_make_client)
    monkeypatch.setattr(token_mod, "make_client", fake_make_client)
    monkeypatch.setattr(token_mod, "_CACHE_DIR", tmp_path)
    return seen_requests


def _intent(side=Side.BUY, qty=10, price=70_000, order_type="limit"):
    return OrderIntent(
        symbol="005930",
        side=side,
        quantity=qty,
        order_type=order_type,
        limit_price=price if order_type == "limit" else None,
        timestamp=datetime.now(UTC),
    )


def test_buy_order_uses_mock_buy_tr_id(monkeypatch, tmp_path):
    seen = _setup(
        monkeypatch,
        tmp_path,
        order_response={
            "rt_cd": "0",
            "msg1": "정상처리",
            "output": {"ODNO": "0000123456", "KRX_FWDG_ORD_ORGNO": "00950"},
        },
    )
    router = KisOrderRouter()
    result = router.submit(_intent(side=Side.BUY))
    assert result.order_id == "0000123456"
    # find the order-cash request
    order_req = next(r for r in seen if r.url.path.endswith("/order-cash"))
    assert order_req.headers["tr_id"] == _ORDER_TR_IDS["mock"][Side.BUY]


def test_sell_order_uses_mock_sell_tr_id(monkeypatch, tmp_path):
    seen = _setup(
        monkeypatch,
        tmp_path,
        order_response={"rt_cd": "0", "output": {"ODNO": "0000999999"}},
    )
    KisOrderRouter().submit(_intent(side=Side.SELL))
    order_req = next(r for r in seen if r.url.path.endswith("/order-cash"))
    assert order_req.headers["tr_id"] == _ORDER_TR_IDS["mock"][Side.SELL]


def test_market_order_sets_dvsn_01(monkeypatch, tmp_path):
    import json

    seen = _setup(
        monkeypatch,
        tmp_path,
        order_response={"rt_cd": "0", "output": {"ODNO": "1"}},
    )
    KisOrderRouter().submit(_intent(order_type="market"))
    order_req = next(r for r in seen if r.url.path.endswith("/order-cash"))
    body = json.loads(order_req.content.decode())
    assert body["ORD_DVSN"] == "01"
    assert body["ORD_UNPR"] == "0"


def test_limit_order_sets_dvsn_00_and_unpr(monkeypatch, tmp_path):
    import json

    seen = _setup(
        monkeypatch,
        tmp_path,
        order_response={"rt_cd": "0", "output": {"ODNO": "1"}},
    )
    KisOrderRouter().submit(_intent(order_type="limit", price=70_500))
    order_req = next(r for r in seen if r.url.path.endswith("/order-cash"))
    body = json.loads(order_req.content.decode())
    assert body["ORD_DVSN"] == "00"
    assert body["ORD_UNPR"] == "70500"


def test_account_fields_propagated(monkeypatch, tmp_path):
    import json

    seen = _setup(
        monkeypatch,
        tmp_path,
        order_response={"rt_cd": "0", "output": {"ODNO": "1"}},
    )
    KisOrderRouter().submit(_intent())
    order_req = next(r for r in seen if r.url.path.endswith("/order-cash"))
    body = json.loads(order_req.content.decode())
    settings = get_settings()
    assert body["CANO"] == settings.account_cano
    assert body["ACNT_PRDT_CD"] == settings.account_prdt
    assert body["PDNO"] == "005930"
    assert body["ORD_QTY"] == "10"


def test_hashkey_header_is_attached(monkeypatch, tmp_path):
    seen = _setup(
        monkeypatch,
        tmp_path,
        order_response={"rt_cd": "0", "output": {"ODNO": "1"}},
        hashkey_response={"HASH": "deadbeef"},
    )
    KisOrderRouter().submit(_intent())
    order_req = next(r for r in seen if r.url.path.endswith("/order-cash"))
    assert order_req.headers["hashkey"] == "deadbeef"


def test_hashkey_fallback_to_lowercase_hash(monkeypatch, tmp_path):
    seen = _setup(
        monkeypatch,
        tmp_path,
        order_response={"rt_cd": "0", "output": {"ODNO": "1"}},
        hashkey_response={"hash": "lowercase"},
    )
    KisOrderRouter().submit(_intent())
    order_req = next(r for r in seen if r.url.path.endswith("/order-cash"))
    assert order_req.headers["hashkey"] == "lowercase"


def test_kis_rejection_raises(monkeypatch, tmp_path):
    _setup(
        monkeypatch,
        tmp_path,
        order_response={"rt_cd": "1", "msg1": "주문수량을 확인해 주세요.", "output": {}},
    )
    with pytest.raises(KisOrderRejected) as excinfo:
        KisOrderRouter().submit(_intent())
    assert excinfo.value.rt_cd == "1"
    assert "주문수량" in excinfo.value.msg
