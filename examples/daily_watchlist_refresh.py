"""Daily watchlist auto-refresh demo — KIS REST 일봉 → PreMarketWatchlistBuilder
+ ThemePairBuilder → PairFollow.pairs 자동 갱신.

매일 09:00 직전 (전날 종가 데이터로) 종목 후보 + 자동 페어 매핑을 생성하는
워크플로우 데모. 실 환경에선 Scheduler.daily_at(8, 50, ...) 으로 호출.

휴장일에도 historical fetch + storage 동작은 검증 가능. KIS REST API 가
필요 (.env 의 KIS 키). 키 없으면 BarStore 의 기존 캐시 데이터로 동작.

실행::

    PYTHONPATH=src .venv/bin/python -m examples.daily_watchlist_refresh
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from ks_ws.market.watchlist_builder import (
    BuildConfig,
    PreMarketWatchlistBuilder,
    universe_from_bar_store,
)
from ks_ws.storage.bars import BarStore
from ks_ws.storage.watchlist import WatchlistStore
from ks_ws.strategies.pair_follow import PairFollowStrategy
from ks_ws.strategies.pair_mapping import ThemePairBuilder


# Example theme classifications (실 사용 시 매일 갱신 / 외부 데이터)
THEME_OF = {
    "A005930": "semiconductor",  # 삼성전자
    "A000660": "semiconductor",  # SK하이닉스
    "A035720": "internet",  # 카카오
    "A035420": "internet",  # NAVER
    "A005380": "auto",  # 현대차
    "A000270": "auto",  # 기아
    "A051910": "battery",  # LG화학
    "A006400": "battery",  # 삼성SDI
}

# Approximate market caps (KRW, 단위: 조). 실 사용 시 KRX/KIS 데이터로 갱신.
MARKET_CAP_OF = {
    "A005930": 500_000_000_000_000,
    "A000660": 100_000_000_000_000,
    "A035720": 25_000_000_000_000,
    "A035420": 30_000_000_000_000,
    "A005380": 60_000_000_000_000,
    "A000270": 40_000_000_000_000,
    "A051910": 40_000_000_000_000,
    "A006400": 30_000_000_000_000,
}


def main() -> int:
    bar_store_path = Path(os.environ.get("KS_WS_DATA_DIR", "data"))
    print(f"=== Bar store: {bar_store_path} ===")
    bar_store = BarStore(bar_store_path)
    universe = universe_from_bar_store(bar_store)
    if not universe:
        print(f"  no symbols found in {bar_store_path}; using THEME_OF keys as universe")
        universe = tuple(THEME_OF.keys())
    print(f"  universe size: {len(universe)}")

    target = date.today()
    with TemporaryDirectory() as tmp:
        wl_store = WatchlistStore(Path(tmp) / "watchlists.sqlite")
        builder = PreMarketWatchlistBuilder(bar_store=bar_store, watchlist_store=wl_store)

        # Build watchlist (top 30 by 거래대금, optionally union with must_include)
        config = BuildConfig(universe=universe, top_n=30, lookback_days=5)
        watchlist = builder.build(target_date=target, config=config)

        print()
        print(f"=== Watchlist for {target} (version={watchlist.version}) ===")
        print(f"  reason: {watchlist.reason}")
        print(f"  size:   {len(watchlist.entries)}")
        for entry in list(watchlist.entries)[:10]:
            tag = entry.meta.get("reason", "")
            score = entry.score
            score_str = f"{score:>20,.0f} KRW" if score > 0 else " " * 24
            print(f"    {entry.symbol:8s} {score_str}  ({tag})")
        if len(watchlist.entries) > 10:
            print(f"    ... and {len(watchlist.entries) - 10} more")

        # Build pairs from watchlist
        theme_builder = ThemePairBuilder(theme_of=THEME_OF, market_cap_of=MARKET_CAP_OF)
        pairs = theme_builder.build_pairs(watchlist)
        multi = theme_builder.build_multi_followers(watchlist, max_followers=2)

        print()
        print("=== Auto-built pairs (theme + market cap) ===")
        if not pairs:
            print("  (no pairs — need ≥2 same-theme symbols in watchlist)")
        for leader, follower in pairs.items():
            print(f"  {leader} → {follower}")

        if multi:
            print()
            print("=== Multi-follower mapping (option) ===")
            for leader, followers in multi.items():
                print(f"  {leader} → {', '.join(followers)}")

        # Instantiate PairFollow with the auto-built pairs
        if pairs:
            strat = PairFollowStrategy(pairs=pairs)
            print()
            print("=== Ready: PairFollowStrategy ===")
            print(f"  pairs configured: {len(strat.pairs)}")
            print(f"  example next session: {target}")
        else:
            print()
            print("  (PairFollow would be skipped today — no eligible pairs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
