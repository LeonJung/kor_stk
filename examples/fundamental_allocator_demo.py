"""FundamentalAllocator 시연 — fundamental 1+2 결합으로 같은 BUY signal 셋이
종목별 macro 상태에 따라 어떻게 다르게 처리되는지.

mock fetchers (실 KIS 호출 없음). 5개 가상 종목 + 같은 신고가매매 BUY signal:

  005930  외인 +12억 / RVOL 3.5x  → 강 macro = boost
  000660  외인 +3억  / RVOL 1.2x  → mild boost
  035420  외인 0     / RVOL 1.0x  → neutral (plain Allocator 와 동일)
  005380  외인 -2억  / RVOL 0.8x  → mild attenuate
  035720  외인 -15억 / RVOL 0.3x  → veto (entry 차단)

실제로는 universe 의 외인 net buy KRW + 일봉 거래량 history 를 KIS REST /
BarStore 에서 fetch. 이 demo 는 wiring 확인용.

Usage::

    PYTHONPATH=src .venv/bin/python -m examples.fundamental_allocator_demo
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ks_ws.domain import Side, Signal
from ks_ws.sources.macro_score import blend_macro_scores
from ks_ws.sources.rvol import compute_rvol_value, score_from_rvol
from ks_ws.strategies.fundamental_allocator import (
    FundamentalAllocator,
    score_from_foreign_flow_krw,
)


@dataclass
class _DemoBar:
    volume: int
    value: int


class _DemoBarStore:
    """In-memory mock BarStore for demo (지난 20일 평균 거래대금)."""

    def __init__(self, per_symbol_avg_value: dict[str, int]) -> None:
        self._bars = {
            sym: [_DemoBar(volume=0, value=avg) for _ in range(20)]
            for sym, avg in per_symbol_avg_value.items()
        }

    def read(self, symbol: str, timeframe: str):
        yield from self._bars.get(symbol, [])


def main() -> None:
    universe = ["005930", "000660", "035420", "005380", "035720"]
    names = {
        "005930": "삼성전자", "000660": "SK하이닉스", "035420": "NAVER",
        "005380": "현대차", "035720": "카카오",
    }
    # 시나리오 입력 (mock)
    foreign_net_krw = {
        "005930": +1_200_000_000,   # +12억 → 1.5
        "000660": +300_000_000,     # +3억  → 1.15
        "035420": 0,                # neutral 1.0
        "005380": -200_000_000,     # -2억  → 0.8
        "035720": -1_500_000_000,   # -15억 → 0.0
    }
    today_value_krw = {
        "005930": 2_800_000_000_000,  # 3.5x of avg 800B
        "000660": 1_320_000_000_000,  # 1.2x of avg 1100B
        "035420": 200_000_000_000,    # 1.0x of avg 200B
        "005380": 480_000_000_000,    # 0.8x of avg 600B
        "035720": 60_000_000_000,     # 0.3x of avg 200B
    }
    avg_value_20d = {
        "005930": 800_000_000_000,
        "000660": 1_100_000_000_000,
        "035420": 200_000_000_000,
        "005380": 600_000_000_000,
        "035720": 200_000_000_000,
    }
    bar_store = _DemoBarStore(avg_value_20d)
    alloc = FundamentalAllocator(max_position_per_symbol=100, min_score=0.5)

    print(f"{'종목':<14} {'외인KRW':>14} {'fScore':>7} {'RVOL':>5} {'rScore':>7} "
          f"{'blend':>6} {'결과':<10} {'qty':>4}")
    print("-" * 80)

    signals: list[Signal] = []
    now = datetime.now(UTC)
    for sym in universe:
        f_score = score_from_foreign_flow_krw(foreign_net_krw[sym])
        rvol = compute_rvol_value(sym, bar_store, today_value_krw[sym], lookback_days=20)
        r_score = score_from_rvol(rvol)
        blended = blend_macro_scores(f_score, r_score)
        alloc.set_macro_score(sym, blended)
        # 같은 신고가매매 BUY signal (confidence 0.8) 모든 종목에 발행
        signals.append(Signal(
            symbol=sym, side=Side.BUY, confidence=0.8,
            strategy="breakout", timestamp=now,
        ))

        # Display blended state (intent 결정 전)
        action_hint = "VETO" if blended < 0.5 else ("BOOST" if blended > 1.05 else "PASS")
        print(f"{sym} {names[sym]:<8} {foreign_net_krw[sym]:>+14,} {f_score:>7.2f} "
              f"{rvol:>5.2f} {r_score:>7.2f} {blended:>6.2f} {action_hint:<10} -")

    intents = alloc.combine(signals)
    print("\n=== OrderIntents (FundamentalAllocator 결정) ===")
    if not intents:
        print("  (모든 BUY 가 veto 되었거나 net 0)")
        return
    intent_by_symbol = {i.symbol: i for i in intents}
    for sym in universe:
        intent = intent_by_symbol.get(sym)
        if intent is None:
            print(f"  {sym} {names[sym]:<8} (vetoed)")
        else:
            print(f"  {sym} {names[sym]:<8} {intent.side.name:<4} qty={intent.quantity}")


if __name__ == "__main__":
    main()
