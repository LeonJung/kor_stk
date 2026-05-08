"""KIS WebSocket realtime feed.

Two pieces:

1. ``fetch_approval_key()`` — REST call to ``/oauth2/Approval`` to obtain
   the WS approval key (separate from the OAuth access token used for
   REST). Cached in memory on the feed instance; fresh approval keys
   can be requested at any time.

2. ``KisRealtimeFeed`` — async manager that opens the WS connection,
   sends pipe-delimited subscribe messages for (tr_id, symbol) pairs,
   and exposes raw frames as an async iterator. Frame parsing for
   specific tr_ids (체결, 호가, etc.) lives in a separate module so the
   transport layer stays thin.

   Auto-reconnect: when the underlying WS connection drops the iterator
   transparently waits ``reconnect_delay``, fetches a fresh approval
   key, reopens the connection, replays every prior subscribe(), and
   resumes yielding frames. After ``max_reconnect_attempts`` consecutive
   failures the iterator gives up (StopAsyncIteration).

KIS WS frame format is *not* JSON — the body is pipe / caret-delimited
text. Tests cover the pieces that can be mocked at the HTTP and message-
construction level; integration with a real WS server is left to the
verify-style example scripts.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator

import httpx
import websockets

from ks_ws.config import Settings, get_settings
from ks_ws.kis.constants import OAUTH_APPROVAL_PATH, REST_BASE_URL, WS_BASE_URL

log = logging.getLogger("ks_ws.kis.realtime")


def fetch_approval_key(settings: Settings | None = None) -> str:
    """Issue a WS approval key. Note the body field name is `secretkey`,
    not `appsecret` — KIS quirk specific to this endpoint."""
    settings = settings or get_settings()
    url = REST_BASE_URL[settings.env] + OAUTH_APPROVAL_PATH
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(
            url,
            json={
                "grant_type": "client_credentials",
                "appkey": settings.app_key,
                "secretkey": settings.app_secret,
            },
            headers={"content-type": "application/json; charset=utf-8"},
        )
        resp.raise_for_status()
        return str(resp.json()["approval_key"])


def build_subscribe_message(
    approval_key: str,
    tr_id: str,
    tr_key: str,
    *,
    register: bool = True,
) -> str:
    """Build the JSON envelope KIS expects for a subscribe / unsubscribe.

    register=True (tr_type=1) registers; register=False (tr_type=2) unsubscribes.
    """
    return json.dumps(
        {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": "1" if register else "2",
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": tr_id, "tr_key": tr_key}},
        }
    )


class KisRealtimeFeed:
    """Connect to the KIS WS endpoint, send subscriptions, yield raw frames.

    Usage::

        feed = KisRealtimeFeed()
        async with feed:
            await feed.subscribe("H0STCNT0", "005930")
            async for frame in feed:
                ... # parse per tr_id
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        auto_reconnect: bool = True,
        reconnect_delay: float = 2.0,
        max_reconnect_attempts: int = 5,
    ) -> None:
        self._settings = settings or get_settings()
        self._approval_key: str | None = None
        self._ws: websockets.ClientConnection | None = None
        # Subscriptions are kept so we can replay them on reconnect.
        self._subscriptions: set[tuple[str, str]] = set()

        self.auto_reconnect = auto_reconnect
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_attempts = max_reconnect_attempts
        self.reconnect_count = 0

    @property
    def approval_key(self) -> str:
        if self._approval_key is None:
            self._approval_key = fetch_approval_key(self._settings)
        return self._approval_key

    async def __aenter__(self) -> "KisRealtimeFeed":
        await self._connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def _connect(self) -> None:
        url = WS_BASE_URL[self._settings.env]
        # Pre-fetch approval key synchronously before opening WS — KIS
        # rejects the first frame if the key isn't ready by handshake.
        _ = self.approval_key
        self._ws = await websockets.connect(url)
        # Replay every stored subscription. Empty on the very first connect.
        for tr_id, tr_key in list(self._subscriptions):
            await self._send_subscribe(tr_id, tr_key, register=True)

    async def _send_subscribe(self, tr_id: str, tr_key: str, *, register: bool) -> None:
        assert self._ws is not None
        msg = build_subscribe_message(self.approval_key, tr_id, tr_key, register=register)
        await self._ws.send(msg)

    async def subscribe(self, tr_id: str, tr_key: str) -> None:
        if self._ws is None:
            raise RuntimeError("KisRealtimeFeed: not connected (use `async with`)")
        self._subscriptions.add((tr_id, tr_key))
        await self._send_subscribe(tr_id, tr_key, register=True)

    async def unsubscribe(self, tr_id: str, tr_key: str) -> None:
        if self._ws is None:
            raise RuntimeError("KisRealtimeFeed: not connected")
        self._subscriptions.discard((tr_id, tr_key))
        await self._send_subscribe(tr_id, tr_key, register=False)

    async def __aiter__(self) -> AsyncIterator[str]:
        if self._ws is None:
            raise RuntimeError("KisRealtimeFeed: not connected")
        attempts = 0
        while True:
            assert self._ws is not None
            try:
                async for raw in self._ws:
                    attempts = 0  # reset on any successful frame
                    yield raw if isinstance(raw, str) else raw.decode("utf-8")
                # Iterator ended normally — server closed cleanly.
                if not self.auto_reconnect:
                    return
            except websockets.ConnectionClosed as e:
                log.warning("KIS WS closed (%s); reconnect=%s", e, self.auto_reconnect)
                if not self.auto_reconnect:
                    return

            attempts += 1
            self.reconnect_count += 1
            if attempts > self.max_reconnect_attempts:
                log.error("WS reconnect gave up after %d attempts", self.max_reconnect_attempts)
                return
            await asyncio.sleep(self.reconnect_delay)
            try:
                # Force a fresh approval key — old one may have expired.
                self._approval_key = None
                await self._connect()
                log.info("WS reconnected (attempt %d)", attempts)
            except Exception as e:
                log.warning("WS reconnect attempt %d failed: %s", attempts, e)
                # Loop will sleep and retry until exhausted.

    @staticmethod
    def parse_frame(frame: str) -> tuple[str, str, list[list[str]]]:
        """Light parser for KIS pipe / caret delimited text frames.

        Layout (no JSON for data frames):
            ``{enc}|{tr_id}|{count}|{caret-separated record} repeated count times``

        Returns ``(tr_id, encryption_flag, records)`` where each record is
        the list of ``^``-separated fields. Whichever field is which is
        tr_id-specific (caller's job).

        JSON control frames (PINGPONG / subscription ack) start with ``{``
        and are returned with tr_id="" and a single record of [raw_json].
        """
        if frame.startswith("{"):
            return ("", "", [[frame]])
        parts = frame.split("|", 3)
        if len(parts) < 4:
            return ("", "", [[frame]])
        enc, tr_id, count_s, payload = parts
        try:
            count = int(count_s)
        except ValueError:
            return (tr_id, enc, [payload.split("^")])
        fields = payload.split("^")
        if count <= 1 or len(fields) % count != 0:
            return (tr_id, enc, [fields])
        per_record = len(fields) // count
        records = [fields[i * per_record : (i + 1) * per_record] for i in range(count)]
        return (tr_id, enc, records)


def subscribe_msg_for_trade(approval_key: str, symbol: str) -> str:
    """Convenience builder for 실시간 주식체결가 (H0STCNT0)."""
    return build_subscribe_message(approval_key, "H0STCNT0", symbol)


def subscribe_msg_for_orderbook(approval_key: str, symbol: str) -> str:
    """Convenience builder for 실시간 주식호가 (H0STASP0)."""
    return build_subscribe_message(approval_key, "H0STASP0", symbol)
