"""LimitUpDetector — emits LimitUpReached when a watched leader stock hits
its daily limit-up price (+30% vs prev close), and LimitUpBroken when
that leader subsequently falls back below it.

KRX 단일 종목 일중 변동폭 ±30% 상한. limit-up = prev_close × 1.30, KRX 호가
단위 절사 (limit_up_price 는 caller 가 미리 계산해 주입). 호가 단위 자동
계산은 별도 RoundFigure utility 의 책임 (현 단계에선 caller 가 정확한
limit_up_price 를 안다고 가정).

State machine: 각 watched symbol 은 NOT_REACHED → REACHED → BROKEN 으로
이동. REACHED 진입 시 LimitUpReached 1회 발행. REACHED 상태에서 best_bid
또는 last price 가 limit_up_price 미만으로 떨어지면 LimitUpBroken 1회 발행
후 NOT_REACHED 로 reset (다시 hit 하면 또 emit 가능).

Detector 는 callback 으로 event 를 publish 한다 (다른 detector 들과 동일
패턴: program_flow / volume_spike).
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from ks_ws.domain import OrderBook, Tick
from ks_ws.events import LimitUpBroken, LimitUpReached


class _State(Enum):
    NOT_REACHED = "not_reached"
    REACHED = "reached"


@dataclass
class _SymbolMeta:
    prev_close: int
    limit_up_price: int
    state: _State = _State.NOT_REACHED


class LimitUpDetector:
    def __init__(
        self,
        *,
        symbols: dict[str, tuple[int, int]],
        emit: Callable[[LimitUpReached | LimitUpBroken], None],
    ) -> None:
        """``symbols`` maps symbol → (prev_close, limit_up_price).
        ``emit`` is called with each LimitUpReached / LimitUpBroken event."""
        self._meta: dict[str, _SymbolMeta] = {
            sym: _SymbolMeta(prev_close=pc, limit_up_price=lu)
            for sym, (pc, lu) in symbols.items()
        }
        self._emit = emit

    def feed_tick(self, tick: Tick) -> None:
        meta = self._meta.get(tick.symbol)
        if meta is None:
            return
        if meta.state == _State.NOT_REACHED and tick.price >= meta.limit_up_price:
            meta.state = _State.REACHED
            self._emit(
                LimitUpReached(
                    symbol=tick.symbol,
                    timestamp=tick.timestamp,
                    limit_up_price=meta.limit_up_price,
                    prev_close=meta.prev_close,
                )
            )
        elif meta.state == _State.REACHED and tick.price < meta.limit_up_price:
            meta.state = _State.NOT_REACHED
            self._emit(
                LimitUpBroken(
                    symbol=tick.symbol,
                    timestamp=tick.timestamp,
                    limit_up_price=meta.limit_up_price,
                    current_price=tick.price,
                )
            )

    def feed_orderbook(self, orderbook: OrderBook) -> None:
        meta = self._meta.get(orderbook.symbol)
        if meta is None or meta.state != _State.REACHED:
            return
        # If best bid drops below limit-up, the lock-up has broken
        if not orderbook.bids:
            return
        best_bid = orderbook.bids[0].price
        if best_bid < meta.limit_up_price:
            meta.state = _State.NOT_REACHED
            self._emit(
                LimitUpBroken(
                    symbol=orderbook.symbol,
                    timestamp=orderbook.timestamp,
                    limit_up_price=meta.limit_up_price,
                    current_price=best_bid,
                )
            )

    def state_of(self, symbol: str) -> str:
        meta = self._meta.get(symbol)
        return meta.state.value if meta else "untracked"
