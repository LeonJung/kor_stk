"""Core domain types for ks_ws.

KRX (한국 주식) 시장 기준:
- 가격은 정수 KRW 원 단위 (소수점 없음).
- 호가창은 10단계 깊이.
- 거래량(volume) = 주식수, 거래대금(value) = sum(price * volume) KRW.

Timestamps are timezone-aware (UTC internally). Convert to KST only at
display / I/O layer. All models are frozen — domain values flow through
the system as immutable records.

Strategy 출력은 Signal (사이즈 정보 없음 — sizing 은 Allocator 책임).
Allocator 출력은 OrderIntent (수량 결정 완료, Risk/Execution 으로).
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


# Market data ---------------------------------------------------------------


class Bar(BaseModel):
    """OHLCV bar for any timeframe ("1m", "5m", "1d", "1w", ...)."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    timestamp: datetime
    timeframe: str
    open: int
    high: int
    low: int
    close: int
    volume: int  # 거래량 (shares)
    value: int  # 거래대금 (KRW)


class Tick(BaseModel):
    """A single trade execution."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    timestamp: datetime
    price: int
    volume: int
    # buy = aggressor lifted the ask; sell = aggressor hit the bid; None if KIS
    # didn't disclose direction for this print.
    aggressor: Side | None = None


class OrderBookLevel(BaseModel):
    model_config = ConfigDict(frozen=True)

    price: int
    volume: int


class OrderBook(BaseModel):
    """Limit order book snapshot. KRX exposes 10 levels each side."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    timestamp: datetime
    bids: tuple[OrderBookLevel, ...]  # best (highest price) first
    asks: tuple[OrderBookLevel, ...]  # best (lowest price) first


# Strategy / Allocator outputs ---------------------------------------------


class Signal(BaseModel):
    """Strategy decision. Sizing is intentionally absent — the Allocator
    derives quantity from confidence and the active risk policy, so
    strategies stay pure (data → intent) and composable.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    side: Side
    confidence: float = Field(ge=0.0, le=1.0)
    urgency: Literal["low", "normal", "high"] = "normal"
    strategy: str  # source strategy name
    timestamp: datetime
    note: str = ""


class OrderIntent(BaseModel):
    """Resolved order from Allocator. Risk checks come next, then Execution."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    side: Side
    quantity: int
    order_type: Literal["market", "limit"] = "market"
    limit_price: int | None = None
    timestamp: datetime
    sources: tuple[str, ...] = ()  # contributing strategy names
