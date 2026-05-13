"""BrokerAdapter — Phase 2.3 다 증권사 abstraction.

memory `project_roadmap` Phase 2.3: KIS / Daishin / etc. broker swap 가능하게.

현재:
- `OrderRouter` ABC (orders.py) = submit() 만 정의
- `KisOrderRouter` 가 유일한 라이브 구현

Phase 2.3 확장:
- BrokerAdapter Protocol (cancel, get_status, account info 추가)
- KisAdapter 가 KisOrderRouter wrap + 추가 메서드
- DaishinAdapter (cycle 10 CYBOS Plus design 활용)
- 다 broker 동시 운영 (종목별 broker 매핑 또는 fail-over)

본 파일 = V1 sketch. 실제 구현은 Phase 2.3 작업.

설계 원칙:
1. `BrokerAdapter` Protocol 만 정의 — runtime check 불가능 (Protocol 한계),
   대신 typing 으로 IDE / mypy 만 도움
2. `KisAdapter` = KisOrderRouter wrap. submit 외 추가 메서드:
   - cancel(order_id) — KIS REST cancel API
   - get_status(order_id) — KIS REST inquire API
   - account_balance() — 현금 잔고 조회
3. DaishinAdapter = stub. Windows CYBOS Plus 가동 후 채움.

Live executor 가 BrokerAdapter 받게 변경 필요 (현재 OrderRouter 받음).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ks_ws.domain import OrderIntent
from ks_ws.orders import KisOrderRouter, SubmittedOrder


@dataclass(frozen=True)
class OrderStatus:
    """주문 상태 정보."""
    order_id: str
    symbol: str
    submitted_at: datetime
    state: str  # "pending" / "partial" / "filled" / "cancelled" / "rejected"
    filled_qty: int = 0
    avg_fill_price: int = 0
    remaining_qty: int = 0


@dataclass(frozen=True)
class AccountBalance:
    cash_krw: int
    stock_value_krw: int  # 평가금액 합
    total_value_krw: int  # 자산총액


class BrokerAdapter(Protocol):
    """다 증권사 통합 인터페이스.

    V1 = submit + cancel + get_status + account_balance.
    V2 (later): subscribe(broker WS fill events), position reconcile.
    """

    name: str  # "kis" / "daishin" / etc

    def submit(self, intent: OrderIntent) -> SubmittedOrder: ...

    def cancel(self, order_id: str) -> bool:
        """Returns True 만 broker 가 cancel 확인. False = 이미 fill 등."""
        ...

    def get_status(self, order_id: str) -> OrderStatus | None: ...

    def account_balance(self) -> AccountBalance | None: ...


class KisAdapter:
    """KisOrderRouter wrap + cancel/get_status/balance API.

    V1 = submit 만 KisOrderRouter 으로 위임. cancel/get_status/balance 는
    Phase 2.3 에서 KIS REST endpoint 호출 추가 구현.
    """

    name = "kis"

    def __init__(self, router: KisOrderRouter | None = None) -> None:
        self._router = router or KisOrderRouter()

    def submit(self, intent: OrderIntent) -> SubmittedOrder:
        return self._router.submit(intent)

    def cancel(self, order_id: str) -> bool:
        # Phase 2.3 TODO: KIS /uapi/domestic-stock/v1/trading/order-rvsecncl
        raise NotImplementedError("KisAdapter.cancel not implemented yet")

    def get_status(self, order_id: str) -> OrderStatus | None:
        # Phase 2.3 TODO: KIS /uapi/domestic-stock/v1/trading/inquire-daily-ccld
        raise NotImplementedError("KisAdapter.get_status not implemented yet")

    def account_balance(self) -> AccountBalance | None:
        # Phase 2.3 TODO: KIS /uapi/domestic-stock/v1/trading/inquire-balance
        raise NotImplementedError("KisAdapter.account_balance not implemented yet")


class DaishinAdapter:
    """Daishin CYBOS Plus broker adapter (Phase 2.3 stub).

    실제 구현은 Windows PC 의 CYBOS Plus 와 LAN 통신 필요 (websocket 또는 별도
    bridge). docs/cybos_plus_realtime_orderbook.md 의 호가 fetcher 와 비슷한
    구조 — order submit 도 같은 통신 채널 활용.
    """

    name = "daishin"

    def submit(self, intent: OrderIntent) -> SubmittedOrder:
        raise NotImplementedError("DaishinAdapter not implemented (Phase 2.3)")

    def cancel(self, order_id: str) -> bool:
        raise NotImplementedError("DaishinAdapter not implemented (Phase 2.3)")

    def get_status(self, order_id: str) -> OrderStatus | None:
        raise NotImplementedError("DaishinAdapter not implemented (Phase 2.3)")

    def account_balance(self) -> AccountBalance | None:
        raise NotImplementedError("DaishinAdapter not implemented (Phase 2.3)")
