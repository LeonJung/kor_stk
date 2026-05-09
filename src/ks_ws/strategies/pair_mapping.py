"""ThemePairBuilder — 자동 leader/follower 페어 생성.

PreMarketWatchlistBuilder 결과 + ``theme_of`` (symbol → theme) mapping +
``market_cap_of`` (symbol → cap KRW) → 같은 테마 안에서 시총 1위 = leader,
2위 = follower 로 자동 페어 생성. PairFollowStrategy 의 ``pairs`` 인자에
주입.

만쥬 운영방식의 자동화 보조: 매일 watchlist 가 갱신되면 페어 매핑도 갱신.
실 사용 시:
1. PreMarketWatchlistBuilder.build() → Watchlist
2. ThemePairBuilder(theme_of, market_cap_of).build_pairs(watchlist)
   → dict[leader, follower]
3. PairFollowStrategy(pairs=...) 재생성 또는 pairs swap

V1 단순 룰: 한 테마에 ≥2 종목이면 시총 desc 정렬 후 (top, 2nd) 페어. 3등
이상은 무시 (간단함). 향후: top → 2등/3등 multi-follower 매핑.
"""

from collections import defaultdict
from collections.abc import Iterable

from ks_ws.storage.watchlist import Watchlist, WatchlistEntry


class ThemePairBuilder:
    def __init__(
        self,
        *,
        theme_of: dict[str, str],
        market_cap_of: dict[str, int],
    ) -> None:
        self.theme_of = dict(theme_of)
        self.market_cap_of = dict(market_cap_of)

    def build_pairs(
        self, watchlist_or_entries: Watchlist | Iterable[WatchlistEntry]
    ) -> dict[str, str]:
        """Return ``{leader: follower}`` from watchlist symbols by theme +
        market cap. Symbols missing theme or market cap are dropped."""
        if isinstance(watchlist_or_entries, Watchlist):
            entries: Iterable[WatchlistEntry] = watchlist_or_entries.entries
        else:
            entries = watchlist_or_entries

        by_theme: dict[str, list[str]] = defaultdict(list)
        for entry in entries:
            theme = self.theme_of.get(entry.symbol)
            if theme is None or entry.symbol not in self.market_cap_of:
                continue
            by_theme[theme].append(entry.symbol)

        pairs: dict[str, str] = {}
        for theme, symbols in by_theme.items():
            if len(symbols) < 2:
                continue
            ranked = sorted(symbols, key=lambda s: self.market_cap_of[s], reverse=True)
            leader, follower = ranked[0], ranked[1]
            pairs[leader] = follower
        return pairs

    def build_multi_followers(
        self,
        watchlist_or_entries: Watchlist | Iterable[WatchlistEntry],
        *,
        max_followers: int = 3,
    ) -> dict[str, list[str]]:
        """Variant: leader → list of followers (2등, 3등, ...) up to
        ``max_followers``. Useful for fanning out signals to multiple
        followers."""
        if max_followers < 1:
            raise ValueError("max_followers must be >= 1")
        if isinstance(watchlist_or_entries, Watchlist):
            entries = list(watchlist_or_entries.entries)
        else:
            entries = list(watchlist_or_entries)

        by_theme: dict[str, list[str]] = defaultdict(list)
        for entry in entries:
            theme = self.theme_of.get(entry.symbol)
            if theme is None or entry.symbol not in self.market_cap_of:
                continue
            by_theme[theme].append(entry.symbol)

        out: dict[str, list[str]] = {}
        for theme, symbols in by_theme.items():
            if len(symbols) < 2:
                continue
            ranked = sorted(symbols, key=lambda s: self.market_cap_of[s], reverse=True)
            leader = ranked[0]
            followers = ranked[1 : 1 + max_followers]
            out[leader] = followers
        return out
