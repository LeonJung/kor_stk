"""ATR helper for strategy-level dynamic TP/SL.

각 strategy 가 entry 시점에 atr_provider(symbol) 호출 → ATR 기반 TP/SL 계산.
사용자 명시 2026-05-15: 모든 strategy 가 ATR 동적 TP/SL 적용. fallback = pct.

Usage in strategy:
    from ks_ws.strategies._atr_helper import resolve_tp_sl

    def _enter(...):
        tp_price, sl_price = resolve_tp_sl(
            entry_price, symbol,
            atr_provider=self.atr_provider, style=self.style,
            fallback_tp_pct=self.take_profit_pct,
            fallback_sl_pct=self.stop_loss_pct,
        )
        # store in _Pos
"""

from __future__ import annotations

from typing import Callable


def resolve_tp_sl(
    entry_price: int,
    symbol: str,
    *,
    atr_provider: Callable[[str], float] | None,
    style: str,
    fallback_tp_pct: float,
    fallback_sl_pct: float,
) -> tuple[int, int]:
    """Return (tp_price, sl_price). ATR provider 있으면 ATR-based, 없으면 fallback pct."""
    if atr_provider is not None:
        try:
            from ks_ws.sources.atr_provider import compute_tp_sl
            atr = float(atr_provider(symbol) or 0)
            if atr > 0:
                return compute_tp_sl(
                    entry_price, atr, style,
                    fallback_tp_pct=fallback_tp_pct,
                    fallback_sl_pct=fallback_sl_pct,
                )
        except Exception:
            pass
    tp = int(entry_price * (1 + fallback_tp_pct / 100))
    sl = int(entry_price * (1 - fallback_sl_pct / 100))
    return tp, sl
