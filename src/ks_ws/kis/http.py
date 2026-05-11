import logging
import time

import httpx

from ks_ws.config import Settings
from ks_ws.kis.constants import REST_BASE_URL
from ks_ws.kis.rate_limit import get_limiter

log = logging.getLogger("ks_ws.kis.http")

# Retry policy for transient KIS server errors. KIS occasionally returns
# 500 (mock especially) and 429 under burst — exponential backoff lets the
# client recover without polluting the failure log.
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3


def make_client(
    settings: Settings,
    *,
    timeout: float = 10.0,
    rate_limit: bool = True,
    retry_on_5xx: bool = True,
) -> httpx.Client:
    """httpx.Client preconfigured for the active KIS environment.

    Adds appkey / appsecret as default headers (KIS requires these on most
    endpoints, including token issuance). Authenticated calls must additionally
    set 'authorization: Bearer {access_token}' and 'tr_id: ...' per request.

    Rate limiting: by default every outgoing request blocks on a process-wide
    token-bucket sized for the active environment (~2 req/sec mock,
    ~20 req/sec live). Pass ``rate_limit=False`` for tests that use mocked
    transports and don't need throttling.
    """
    event_hooks: dict[str, list] = {}
    if rate_limit:
        # Per-account limiter (key = app_key prefix). Allows multiple keys
        # in one process to throttle independently (5/11-12 multi-account).
        limiter = get_limiter(settings.env, key=settings.app_key[:8])

        def _before(_request: httpx.Request) -> None:
            limiter.acquire()

        event_hooks["request"] = [_before]

    transport = (
        _RetryTransport(max_retries=_MAX_RETRIES) if retry_on_5xx
        else httpx.HTTPTransport()
    )

    return httpx.Client(
        base_url=REST_BASE_URL[settings.env],
        timeout=timeout,
        headers={
            "content-type": "application/json; charset=utf-8",
            "appkey": settings.app_key,
            "appsecret": settings.app_secret,
        },
        event_hooks=event_hooks,
        transport=transport,
    )


class _RetryTransport(httpx.HTTPTransport):
    """httpx transport that retries 429/5xx responses with exponential backoff
    (0.5s, 1.0s, 2.0s). Network errors (httpx.ReadTimeout / httpx.ConnectError)
    also retry."""

    def __init__(self, max_retries: int = _MAX_RETRIES) -> None:
        super().__init__()
        self.max_retries = max_retries

    def handle_request(self, request: httpx.Request) -> httpx.Response:  # type: ignore[override]
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = super().handle_request(request)
                if resp.status_code in _RETRY_STATUSES and attempt < self.max_retries:
                    backoff = 0.5 * (2 ** attempt)
                    log.warning(
                        "KIS %s %s → %d, retry %d/%d in %.1fs",
                        request.method, request.url.path, resp.status_code,
                        attempt + 1, self.max_retries, backoff,
                    )
                    resp.close()
                    time.sleep(backoff)
                    continue
                return resp
            except (httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError) as e:
                last_exc = e
                if attempt < self.max_retries:
                    backoff = 0.5 * (2 ** attempt)
                    log.warning(
                        "KIS %s %s exception (%s), retry %d/%d in %.1fs",
                        request.method, request.url.path, type(e).__name__,
                        attempt + 1, self.max_retries, backoff,
                    )
                    time.sleep(backoff)
                    continue
                raise
        if last_exc:
            raise last_exc
        return resp  # type: ignore[possibly-unbound]
