import logging
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from pydantic import BaseModel

from ks_ws.config import Settings, get_settings
from ks_ws.kis.constants import OAUTH_TOKEN_PATH
from ks_ws.kis.http import make_client

log = logging.getLogger("ks_ws.auth")

# Refresh slightly before expiry so requests in flight don't race the boundary.
_REFRESH_MARGIN = timedelta(minutes=5)
_CACHE_DIR = Path(__file__).resolve().parents[3] / "data"
_lock = threading.Lock()


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    access_token_token_expired: str = ""


class CachedToken(BaseModel):
    access_token: str
    expires_at: datetime
    env: str

    @classmethod
    def from_response(
        cls,
        env: str,
        resp: TokenResponse,
        *,
        now: datetime | None = None,
    ) -> "CachedToken":
        now = now or datetime.now(UTC)
        return cls(
            access_token=resp.access_token,
            expires_at=now + timedelta(seconds=resp.expires_in),
            env=env,
        )

    def is_valid(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        return now + _REFRESH_MARGIN < self.expires_at


def _cache_path(env: str) -> Path:
    return _CACHE_DIR / f"token-{env}.json"


def _read_cache(env: str) -> CachedToken | None:
    path = _cache_path(env)
    if not path.exists():
        return None
    try:
        return CachedToken.model_validate_json(path.read_text())
    except Exception as e:
        log.warning("token cache unreadable, ignoring: %s", e)
        return None


def _write_cache(token: CachedToken) -> None:
    path = _cache_path(token.env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token.model_dump_json())
    path.chmod(0o600)


def fetch_token(settings: Settings, client: httpx.Client | None = None) -> TokenResponse:
    """Issue a fresh access token from KIS. Does not touch the cache."""
    owns_client = client is None
    if client is None:
        client = make_client(settings)
    try:
        resp = client.post(
            OAUTH_TOKEN_PATH,
            json={
                "grant_type": "client_credentials",
                "appkey": settings.app_key,
                "appsecret": settings.app_secret,
            },
        )
        resp.raise_for_status()
        return TokenResponse.model_validate(resp.json())
    finally:
        if owns_client:
            client.close()


def get_token(settings: Settings | None = None, *, force_refresh: bool = False) -> str:
    """Return a valid access token, using disk cache when possible.

    KIS rate-limits token issuance (~1/min), so callers should reuse this
    rather than calling fetch_token directly.
    """
    settings = settings or get_settings()
    with _lock:
        if not force_refresh:
            cached = _read_cache(settings.env)
            if cached and cached.is_valid():
                return cached.access_token
        log.info("issuing new KIS token (env=%s)", settings.env)
        resp = fetch_token(settings)
        cached = CachedToken.from_response(settings.env, resp)
        _write_cache(cached)
        return cached.access_token
