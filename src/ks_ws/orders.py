"""OrderRouter — submits OrderIntents to a broker.

``MockOrderRouter`` records submissions in memory; suitable for tests,
backtest dry runs, and pre-key development. ``KisOrderRouter`` calls
``/uapi/domestic-stock/v1/trading/order-cash`` against the configured
KIS environment (mock vs live differs only by base URL and tr_id
prefix — V for mock, T for live).

The router is the *submission* point — fill semantics belong elsewhere
(BacktestDriver simulates fills against bars; a future live executor
will receive fills via WS or polling REST). This keeps the router a
thin interface.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime

from ks_ws.auth.token import get_token
from ks_ws.config import Settings, get_settings
from ks_ws.domain import OrderIntent, Side
from ks_ws.kis.http import make_client

log = logging.getLogger("ks_ws.orders")

_ORDER_CASH_PATH = "/uapi/domestic-stock/v1/trading/order-cash"
_HASHKEY_PATH = "/uapi/hashkey"

# tr_id is environment- and side-dependent. KIS keeps order tr_ids on the
# V/T axis (V=mock, T=live).
_ORDER_TR_IDS: dict[str, dict[Side, str]] = {
    "mock": {Side.BUY: "VTTC0012U", Side.SELL: "VTTC0011U"},
    "live": {Side.BUY: "TTTC0012U", Side.SELL: "TTTC0011U"},
}


@dataclass(frozen=True)
class SubmittedOrder:
    order_id: str
    intent: OrderIntent
    submitted_at: datetime


class OrderRouter(ABC):
    @abstractmethod
    def submit(self, intent: OrderIntent) -> SubmittedOrder:
        """Submit an order to the broker. Returns SubmittedOrder with the
        broker's order ID. Implementations decide whether to block on
        confirmation or return as soon as the broker accepts the request.
        """


class MockOrderRouter(OrderRouter):
    """Records every submitted intent in memory. No fill simulation —
    pair with a backtest driver or future live executor for that.
    """

    def __init__(self) -> None:
        self._submitted: list[SubmittedOrder] = []
        self._counter = 0

    def submit(self, intent: OrderIntent) -> SubmittedOrder:
        self._counter += 1
        order = SubmittedOrder(
            order_id=f"mock-{self._counter}",
            intent=intent,
            submitted_at=datetime.now(UTC),
        )
        self._submitted.append(order)
        return order

    @property
    def submitted(self) -> list[SubmittedOrder]:
        return list(self._submitted)

    def clear(self) -> None:
        self._submitted.clear()
        self._counter = 0


class KisOrderRejected(Exception):
    """KIS responded with a non-success rt_cd to the order request."""

    def __init__(self, rt_cd: str, msg: str, intent: OrderIntent) -> None:
        super().__init__(f"KIS order rejected (rt_cd={rt_cd}): {msg}")
        self.rt_cd = rt_cd
        self.msg = msg
        self.intent = intent


class KisOrderRouter(OrderRouter):
    """Live KIS order submission. Computes a hashkey for the body, signs
    the request with the cached access token, and POSTs to order-cash.

    Mock vs live: only base URL and tr_id prefix change, both handled by
    Settings. Hashkey is required by KIS for body-bearing endpoints —
    we let KIS itself compute it via /uapi/hashkey rather than rolling
    our own (which would diverge from any future spec change).

    Raises KisOrderRejected on non-zero rt_cd. Network / 4xx / 5xx
    errors propagate as the underlying httpx exception.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def submit(self, intent: OrderIntent) -> SubmittedOrder:
        s = self._settings
        tr_id = _ORDER_TR_IDS[s.env][intent.side]

        body = {
            "CANO": s.account_cano,
            "ACNT_PRDT_CD": s.account_prdt,
            "PDNO": intent.symbol,
            "ORD_DVSN": "01" if intent.order_type == "market" else "00",
            "ORD_QTY": str(intent.quantity),
            "ORD_UNPR": str(intent.limit_price or 0),
        }

        token = get_token(s)
        client = make_client(s)
        try:
            hashkey = self._compute_hashkey(client, body)
            resp = client.post(
                _ORDER_CASH_PATH,
                json=body,
                headers={
                    "authorization": f"Bearer {token}",
                    "tr_id": tr_id,
                    "custtype": "P",
                    "hashkey": hashkey,
                    "content-type": "application/json; charset=utf-8",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        finally:
            client.close()

        if data.get("rt_cd") != "0":
            raise KisOrderRejected(
                rt_cd=str(data.get("rt_cd")),
                msg=str(data.get("msg1") or "unknown"),
                intent=intent,
            )

        out = data.get("output") or {}
        # ODNO = 주문번호; KRX_FWDG_ORD_ORGNO = 한국거래소 전송 주문조직 번호
        order_id = out.get("ODNO") or "unknown"
        log.info(
            "KIS order accepted: %s %s %d %s id=%s",
            intent.symbol,
            intent.side,
            intent.quantity,
            intent.order_type,
            order_id,
        )
        return SubmittedOrder(
            order_id=str(order_id),
            intent=intent,
            submitted_at=datetime.now(UTC),
        )

    @staticmethod
    def _compute_hashkey(client, body: dict) -> str:
        resp = client.post(_HASHKEY_PATH, json=body)
        resp.raise_for_status()
        data = resp.json()
        # KIS docs show the hash under different keys across versions —
        # prefer the documented "HASH", fall back to nested forms.
        if "HASH" in data:
            return str(data["HASH"])
        if "hash" in data:
            return str(data["hash"])
        body_obj = data.get("BODY") or {}
        if "hash" in body_obj:
            return str(body_obj["hash"])
        raise RuntimeError(f"hashkey response missing hash field: {data}")
