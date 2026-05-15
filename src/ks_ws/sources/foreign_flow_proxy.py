"""ForeignFlowProxy — 일봉 데이터로부터 외인 spike 추정 → ForeignNetBuy event.

KIS mock 환경의 historical pagination 한계 (최근 30일만) 우회를 위한 proxy.
사용자 룰 (2026-05-15): 옵션 B = 거래대금 spike + 가격 spike 로 외인 매수 추정.

룰:
- (acml_tr_pbmn, 거래대금) 가 5일 평균 대비 **N배 spike** (default 1.5x)
- 일봉 close prev_close 대비 **+M%** 이상 (default +3%)
- 두 조건 모두 충족 시 → 그날 외인 매수 spike 가정
- delta_krw = 거래대금 × estimated_foreign_share (default 0.30 = 30%)

ForeignNetBuy event 의 source 와 동일 형식 emit. 정확도 60-70% 추정 (실제 외인
데이터 검증 시 비교 필요).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ks_ws.events import ForeignNetBuy

if TYPE_CHECKING:
    from ks_ws.bus import EventBus
    from ks_ws.domain import Bar


@dataclass
class ProxyConfig:
    volume_ratio: float = 1.5    # 거래대금 / 5일평균 ≥ 이 배수
    price_jump_pct: float = 3.0  # 일봉 +N% 이상 상승
    foreign_share: float = 0.30  # 거래대금 × 이 비율 = 추정 외인 매수 KRW


def emit_proxy_events(
    bars_per_symbol: dict[str, list[Bar]],
    bus: EventBus,
    *,
    config: ProxyConfig | None = None,
    lookback: int = 5,
) -> int:
    """각 종목의 일봉 시계열을 walk → spike 발생 시 ForeignNetBuy event publish.

    Returns: emit 된 event 개수.
    """
    cfg = config or ProxyConfig()
    emitted = 0
    for symbol, bars in bars_per_symbol.items():
        if len(bars) < lookback + 1:
            continue
        bars_sorted = sorted(bars, key=lambda b: b.timestamp)
        for i in range(lookback, len(bars_sorted)):
            cur = bars_sorted[i]
            prev = bars_sorted[i - 1]
            window = bars_sorted[i - lookback : i]
            if not window or prev.close <= 0:
                continue
            avg_value = sum(b.value for b in window) / len(window)
            if avg_value <= 0 or cur.value <= 0:
                continue
            vol_ratio = cur.value / avg_value
            price_pct = (cur.close - prev.close) / prev.close * 100
            if vol_ratio < cfg.volume_ratio:
                continue
            if price_pct < cfg.price_jump_pct:
                continue
            est_foreign_krw = int(cur.value * cfg.foreign_share)
            event = ForeignNetBuy(
                symbol=symbol,
                timestamp=cur.timestamp,
                delta_krw=est_foreign_krw,
                window_seconds=86_400,  # 1 day
            )
            bus.publish(event)
            emitted += 1
    return emitted


def emit_proxy_events_from_bars(
    all_bars: Iterable[Bar],
    bus: EventBus,
    *,
    config: ProxyConfig | None = None,
    lookback: int = 5,
) -> int:
    """편의 helper: bars iter → 종목별 grouping → emit_proxy_events."""
    from collections import defaultdict
    by_sym: dict[str, list] = defaultdict(list)
    for b in all_bars:
        by_sym[b.symbol].append(b)
    return emit_proxy_events(by_sym, bus, config=config, lookback=lookback)
