from datetime import UTC, date, datetime

import httpx
import pytest

from ks_ws.auth import token as token_mod
from ks_ws.market import kis_rest as kis_rest_mod
from ks_ws.market.kis_rest import fetch_daily_bars


def _make_handler(daily_response: dict | None = None) -> httpx.MockTransport:
    if daily_response is None:
        daily_response = {
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "정상처리되었습니다.",
            "output2": [
                {
                    "stck_bsop_date": "20251115",
                    "stck_oprc": "70000",
                    "stck_hgpr": "70500",
                    "stck_lwpr": "69500",
                    "stck_clpr": "70200",
                    "acml_vol": "10000000",
                    "acml_tr_pbmn": "702000000000",
                },
                {
                    "stck_bsop_date": "20251114",
                    "stck_oprc": "69500",
                    "stck_hgpr": "70000",
                    "stck_lwpr": "69000",
                    "stck_clpr": "69800",
                    "acml_vol": "9000000",
                    "acml_tr_pbmn": "628200000000",
                },
            ],
        }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(
                200,
                json={
                    "access_token": "test_token",
                    "token_type": "Bearer",
                    "expires_in": 86400,
                    "access_token_token_expired": "2099-01-01 00:00:00",
                },
            )
        if request.url.path.endswith("/inquire-daily-itemchartprice"):
            return httpx.Response(200, json=daily_response)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def fake_kis(monkeypatch, tmp_path):
    """Replace make_client (in both token and kis_rest modules) with a
    MockTransport-backed client, and isolate the token disk cache."""
    transport = _make_handler()

    def fake_make_client(settings, **_kw):
        return httpx.Client(
            transport=transport,
            base_url="https://mock",
            headers={
                "appkey": settings.app_key,
                "appsecret": settings.app_secret,
            },
        )

    monkeypatch.setattr(kis_rest_mod, "make_client", fake_make_client)
    monkeypatch.setattr(token_mod, "make_client", fake_make_client)
    monkeypatch.setattr(token_mod, "_CACHE_DIR", tmp_path)


def test_fetch_daily_bars_returns_oldest_first(fake_kis):
    bars = fetch_daily_bars("005930", start=date(2025, 11, 14), end=date(2025, 11, 15))
    assert len(bars) == 2
    assert bars[0].timestamp.date() == date(2025, 11, 14)
    assert bars[1].timestamp.date() == date(2025, 11, 15)


def test_fetch_daily_bars_field_mapping(fake_kis):
    bars = fetch_daily_bars("005930", start=date(2025, 11, 14), end=date(2025, 11, 15))
    latest = bars[1]  # 2025-11-15
    assert latest.symbol == "005930"
    assert latest.timeframe == "1d"
    assert latest.open == 70_000
    assert latest.high == 70_500
    assert latest.low == 69_500
    assert latest.close == 70_200
    assert latest.volume == 10_000_000
    assert latest.value == 702_000_000_000
    assert latest.timestamp == datetime(2025, 11, 15, tzinfo=UTC)


def test_fetch_daily_bars_skips_empty_rows(monkeypatch, tmp_path):
    """KIS sometimes returns empty rows when the range exceeds history."""
    transport = httpx.MockTransport(
        lambda req: (
            httpx.Response(
                200,
                json={
                    "access_token": "test_token",
                    "token_type": "Bearer",
                    "expires_in": 86400,
                    "access_token_token_expired": "2099-01-01 00:00:00",
                },
            )
            if req.url.path == "/oauth2/tokenP"
            else httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "output2": [
                        {
                            "stck_bsop_date": "20251115",
                            "stck_oprc": "70000",
                            "stck_hgpr": "70500",
                            "stck_lwpr": "69500",
                            "stck_clpr": "70200",
                            "acml_vol": "10000000",
                            "acml_tr_pbmn": "702000000000",
                        },
                        {
                            "stck_bsop_date": "",  # empty row
                            "stck_oprc": "0",
                            "stck_hgpr": "0",
                            "stck_lwpr": "0",
                            "stck_clpr": "0",
                            "acml_vol": "0",
                            "acml_tr_pbmn": "0",
                        },
                    ],
                },
            )
        )
    )

    def fake_make_client(settings, **_kw):
        return httpx.Client(
            transport=transport,
            base_url="https://mock",
            headers={"appkey": settings.app_key, "appsecret": settings.app_secret},
        )

    monkeypatch.setattr(kis_rest_mod, "make_client", fake_make_client)
    monkeypatch.setattr(token_mod, "make_client", fake_make_client)
    monkeypatch.setattr(token_mod, "_CACHE_DIR", tmp_path)

    bars = fetch_daily_bars("005930", start=date(2025, 11, 14), end=date(2025, 11, 15))
    assert len(bars) == 1
    assert bars[0].timestamp.date() == date(2025, 11, 15)


def test_fetch_daily_bars_handles_rt_cd_error(monkeypatch, tmp_path, caplog):
    """Non-success rt_cd should log a warning and return whatever rows came
    through (output2 may be empty)."""
    transport = httpx.MockTransport(
        lambda req: (
            httpx.Response(
                200,
                json={
                    "access_token": "test_token",
                    "token_type": "Bearer",
                    "expires_in": 86400,
                    "access_token_token_expired": "2099-01-01 00:00:00",
                },
            )
            if req.url.path == "/oauth2/tokenP"
            else httpx.Response(
                200,
                json={"rt_cd": "1", "msg1": "조회된 데이터가 없습니다.", "output2": []},
            )
        )
    )

    def fake_make_client(settings, **_kw):
        return httpx.Client(
            transport=transport,
            base_url="https://mock",
            headers={"appkey": settings.app_key, "appsecret": settings.app_secret},
        )

    monkeypatch.setattr(kis_rest_mod, "make_client", fake_make_client)
    monkeypatch.setattr(token_mod, "make_client", fake_make_client)
    monkeypatch.setattr(token_mod, "_CACHE_DIR", tmp_path)

    with caplog.at_level("WARNING", logger="ks_ws.market.kis_rest"):
        bars = fetch_daily_bars("005930", start=date(2025, 11, 14), end=date(2025, 11, 15))
    assert bars == []
    assert any("rt_cd=1" in m for m in caplog.messages)


def test_fetch_weekly_period_sets_timeframe(fake_kis):
    bars = fetch_daily_bars("005930", start=date(2025, 1, 1), end=date(2025, 11, 15), period="W")
    assert all(b.timeframe == "1w" for b in bars)
