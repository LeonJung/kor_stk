from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ks_ws.auth import token as token_mod
from ks_ws.auth.token import CachedToken, get_token


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(token_mod, "_CACHE_DIR", tmp_path)


@pytest.fixture
def fake_kis(monkeypatch):
    """Replace make_client so calls hit a MockTransport instead of the network."""
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(
            200,
            json={
                "access_token": f"token-{calls['count']}",
                "token_type": "Bearer",
                "expires_in": 86400,
                "access_token_token_expired": "2099-01-01 00:00:00",
            },
        )

    transport = httpx.MockTransport(handler)

    def fake_make_client(_settings, **_kw):
        return httpx.Client(transport=transport, base_url="https://mock")

    monkeypatch.setattr(token_mod, "make_client", fake_make_client)
    return calls


def test_issues_token_when_cache_empty(fake_kis):
    assert get_token() == "token-1"
    assert fake_kis["count"] == 1


def test_reuses_cached_token(fake_kis):
    get_token()
    get_token()
    assert fake_kis["count"] == 1


def test_force_refresh_bypasses_cache(fake_kis):
    get_token()
    get_token(force_refresh=True)
    assert fake_kis["count"] == 2


def test_expired_cache_triggers_refresh(fake_kis):
    expired = CachedToken(
        access_token="old",
        expires_at=datetime.now(UTC) - timedelta(hours=1),
        env="mock",
    )
    p = token_mod._cache_path("mock")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(expired.model_dump_json())

    assert get_token() == "token-1"
    assert fake_kis["count"] == 1


def test_cached_token_is_valid_within_expiry():
    fresh = CachedToken(
        access_token="x",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        env="mock",
    )
    assert fresh.is_valid() is True


def test_cached_token_invalid_inside_refresh_margin():
    near_expiry = CachedToken(
        access_token="x",
        expires_at=datetime.now(UTC) + timedelta(minutes=2),
        env="mock",
    )
    assert near_expiry.is_valid() is False
