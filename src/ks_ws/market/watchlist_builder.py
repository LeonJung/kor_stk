"""PreMarketWatchlistBuilder — 매일 09:00 직전 종목 후보 자동 선정.

만쥬 운영방식 (블로그 cross-check): 매일 전날 저녁 ~ 다음날 아침 사이 20-30개
종목 후보 사전 선정. 거래대금 + 재료 (테마/뉴스) 우선, 펀더멘탈 참고용.

V1 단순 버전: BarStore 의 전일 daily bar 데이터에서 거래대금 (value) 상위
N개 자동 선정. 향후 확장:
- 시간외 단일가 상승 종목 추가 (KIS API 시간외 endpoint)
- 미국장 상승 ADR/연관 종목 추가
- 뉴스 NLP 기반 테마 detection
- 사용자가 제공하는 must_include / must_exclude list

Strategy 들 (OpeningMomentum, PairFollow) 이 매일 시작 시 watchlist 를 read 해
universe 로 사용. Scheduler 의 daily_at(8, 50, ...) hook 으로 호출.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from ks_ws.storage.bars import BarStore
from ks_ws.storage.watchlist import (
    Watchlist,
    WatchlistEntry,
    WatchlistStore,
    now_utc,
)


@dataclass(frozen=True)
class BuildConfig:
    universe: tuple[str, ...]
    top_n: int = 30
    timeframe: str = "1d"
    lookback_days: int = 5  # search back this many calendar days for the latest bar
    must_include: tuple[str, ...] = ()
    must_exclude: tuple[str, ...] = ()


class PreMarketWatchlistBuilder:
    def __init__(
        self,
        *,
        bar_store: BarStore,
        watchlist_store: WatchlistStore,
    ) -> None:
        self.bar_store = bar_store
        self.watchlist_store = watchlist_store

    def build(self, *, target_date: date, config: BuildConfig) -> Watchlist:
        """Compute the watchlist for ``target_date`` (the trading day we're
        preparing for). Uses the latest daily bar within ``lookback_days``
        before ``target_date`` to rank by trading value (거래대금)."""
        if not config.universe:
            raise ValueError("universe must not be empty")
        if config.top_n <= 0:
            raise ValueError("top_n must be positive")

        excluded = set(config.must_exclude)
        candidates: list[tuple[str, int]] = []  # (symbol, value)

        for symbol in config.universe:
            if symbol in excluded:
                continue
            latest_value = self._latest_trading_value(symbol, config, target_date)
            if latest_value is not None:
                candidates.append((symbol, latest_value))

        candidates.sort(key=lambda x: x[1], reverse=True)
        top = candidates[: config.top_n]

        # Merge in must_include (preserve order, dedupe)
        seen: set[str] = set()
        entries: list[WatchlistEntry] = []
        for sym in config.must_include:
            if sym in excluded or sym in seen:
                continue
            entries.append(WatchlistEntry(symbol=sym, score=0.0, meta={"reason": "must_include"}))
            seen.add(sym)
        for sym, value in top:
            if sym in seen:
                continue
            entries.append(
                WatchlistEntry(
                    symbol=sym,
                    score=float(value),
                    meta={"reason": "top_trading_value"},
                )
            )
            seen.add(sym)

        version = self.watchlist_store.next_version_for(target_date)
        watchlist = Watchlist(
            date=target_date,
            version=version,
            generated_at=now_utc(),
            reason=f"top {config.top_n} by 거래대금 + must_include",
            entries=tuple(entries),
        )
        self.watchlist_store.save(watchlist)
        return watchlist

    def _latest_trading_value(
        self, symbol: str, config: BuildConfig, target_date: date
    ) -> int | None:
        """Return the value (거래대금) of the most recent bar within
        [target_date - lookback_days, target_date)."""
        start = datetime.combine(
            target_date - timedelta(days=config.lookback_days), datetime.min.time()
        ).replace(tzinfo=UTC)
        end = datetime.combine(target_date, datetime.min.time()).replace(tzinfo=UTC)
        latest: int | None = None
        for bar in self.bar_store.read(symbol, config.timeframe, start=start, end=end):
            latest = bar.value
        return latest


def universe_from_bar_store(bar_store: BarStore, *, timeframe: str = "1d") -> tuple[str, ...]:
    """Helper: discover all symbols present in the bar_store for given timeframe.

    Useful when caller wants the universe = "all symbols we have data for".
    """
    base = bar_store.root / "bars" / timeframe
    if not base.exists():
        return ()
    symbols = sorted(p.name for p in base.iterdir() if p.is_dir())
    return tuple(symbols)


def aggregate_by_theme(
    entries: Iterable[WatchlistEntry], theme_of: dict[str, str]
) -> dict[str, list[str]]:
    """Group watchlist symbols by theme using a {symbol: theme} mapping.
    Useful for building leader-follower pairs from a daily watchlist."""
    out: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        theme = theme_of.get(entry.symbol)
        if theme:
            out[theme].append(entry.symbol)
    return dict(out)
