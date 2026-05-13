"""Per-strategy realized PnL aggregation from the Ledger.

각 strategy 가 얼마나 PnL 을 기여했는지 분리 집계한다. Ledger 의 ``orders``
테이블에는 ``sources`` 컬럼 (CSV 형태로 strategy name 들) 이 있고, ``fills``
테이블엔 fill 단위 가격/수량이 있어 둘을 join 해서 strategy 별 trade list 를
재구성할 수 있다. 한 order 에 N strategy 가 기여했으면 PnL 을 weight 로
분배 (default = equal weight).

가상돈 매매 → Claude 회고 루프 (사용자 D-9 결정) 의 데이터 source. 사용자가
"이 strategy 는 잘 굴러가는데 저건 별로네" 를 즉시 판단할 수 있게 한다.

Realized PnL 만 계산 (open position 의 unrealized 는 제외). Side 는 BUY = 매수,
SELL = 매도이고 매수→매도 cycle 이 완성되어야 PnL 이 잡힌다 (FIFO matching).
"""

from collections import defaultdict
from dataclasses import dataclass

from ks_ws.storage.ledger import Ledger


@dataclass(frozen=True)
class StrategyStats:
    strategy: str
    trades: int
    wins: int
    losses: int
    realized_pnl_krw: float
    win_rate: float
    avg_win_krw: float
    avg_loss_krw: float
    expectancy_krw: float

    @classmethod
    def empty(cls, strategy: str) -> "StrategyStats":
        return cls(
            strategy=strategy,
            trades=0,
            wins=0,
            losses=0,
            realized_pnl_krw=0.0,
            win_rate=0.0,
            avg_win_krw=0.0,
            avg_loss_krw=0.0,
            expectancy_krw=0.0,
        )


@dataclass(frozen=True)
class _Lot:
    """An open lot of shares from a single fill, awaiting close."""

    order_id: str
    sources: tuple[str, ...]
    quantity: int
    price: int


def total_realized_pnl_krw(ledger: Ledger) -> int:
    """Sum of all realized trade PnL across strategies. Used by LiveExecutor
    to feed Risk.daily_loss_limit_krw circuit (live update from ledger)."""
    stats = aggregate_strategy_pnl(ledger)
    return int(sum(s.realized_pnl_krw for s in stats.values()))


def aggregate_strategy_pnl(ledger: Ledger) -> dict[str, StrategyStats]:
    """Walk all fills in time order, FIFO match BUY → SELL per symbol, then
    distribute the realized PnL across the contributing strategies of the
    SELL order (the order that closes the position). Each closing trade
    counts as 1 trade per contributing strategy.

    Why distribute on the closing order? — the closing decision is what
    realized the PnL. (Could alternatively split across opening
    contributions; this is a project convention, easy to swap later.)
    """
    fills = ledger.list_fills()
    orders_by_id = {row["order_id"]: row for row in ledger.list_orders()}

    open_lots: dict[str, list[_Lot]] = defaultdict(list)
    realized: dict[str, list[float]] = defaultdict(list)  # strategy -> list of trade pnls

    for f in fills:
        symbol = f["symbol"]
        side = f["side"]
        qty = int(f["quantity"])
        price = int(f["price"])
        order = orders_by_id.get(f["order_id"], {})
        sources_str = order.get("sources") or ""
        sources = tuple(s for s in sources_str.split(",") if s)

        if side == "buy":
            open_lots[symbol].append(
                _Lot(order_id=f["order_id"], sources=sources, quantity=qty, price=price)
            )
        else:  # sell — close oldest lots FIFO
            remaining = qty
            trade_pnl = 0.0
            while remaining > 0 and open_lots[symbol]:
                lot = open_lots[symbol][0]
                take = min(remaining, lot.quantity)
                trade_pnl += (price - lot.price) * take
                remaining -= take
                if take == lot.quantity:
                    open_lots[symbol].pop(0)
                else:
                    open_lots[symbol][0] = _Lot(
                        order_id=lot.order_id,
                        sources=lot.sources,
                        quantity=lot.quantity - take,
                        price=lot.price,
                    )
            # Distribute trade_pnl equally across the closing order's sources
            close_sources = sources or ("(unknown)",)
            share = trade_pnl / len(close_sources)
            for s in close_sources:
                realized[s].append(share)

    return {
        strategy: _stats(strategy, pnls) for strategy, pnls in realized.items()
    }


def _stats(strategy: str, pnls: list[float]) -> StrategyStats:
    if not pnls:
        return StrategyStats.empty(strategy)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    trades = len(pnls)
    total = sum(pnls)
    return StrategyStats(
        strategy=strategy,
        trades=trades,
        wins=len(wins),
        losses=len(losses),
        realized_pnl_krw=total,
        win_rate=len(wins) / trades if trades else 0.0,
        avg_win_krw=sum(wins) / len(wins) if wins else 0.0,
        avg_loss_krw=sum(losses) / len(losses) if losses else 0.0,
        expectancy_krw=total / trades if trades else 0.0,
    )
