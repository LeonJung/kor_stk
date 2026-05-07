import httpx

from ks_ws.config import Settings
from ks_ws.kis.constants import REST_BASE_URL


def make_client(settings: Settings, *, timeout: float = 10.0) -> httpx.Client:
    """httpx.Client preconfigured for the active KIS environment.

    Adds appkey / appsecret as default headers (KIS requires these on most
    endpoints, including token issuance). Authenticated calls must additionally
    set 'authorization: Bearer {access_token}' and 'tr_id: ...' per request.
    """
    return httpx.Client(
        base_url=REST_BASE_URL[settings.env],
        timeout=timeout,
        headers={
            "content-type": "application/json; charset=utf-8",
            "appkey": settings.app_key,
            "appsecret": settings.app_secret,
        },
    )
