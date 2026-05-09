"""Tests for WatchlistStore + PreMarketWatchlistBuilder."""

from datetime import UTC, date, datetime, timedelta

import pytest

from ks_ws.domain import Bar
from ks_ws.market.watchlist_builder import (
    BuildConfig,
    PreMarketWatchlistBuilder,
    aggregate_by_theme,
    universe_from_bar_store,
)
from ks_ws.storage.bars import BarStore
from ks_ws.storage.watchlist import (
    Watchlist,
    WatchlistEntry,
    WatchlistStore,
    now_utc,
)


def _bar(symbol: str, day: date, value: int):
    ts = datetime.combine(day, datetime.min.time()).replace(tzinfo=UTC)
    return Bar(
        symbol=symbol,
        timestamp=ts,
        timeframe="1d",
        open=10000,
        high=10100,
        low=9900,
        close=10050,
        volume=value // 10000,
        value=value,
    )


# WatchlistStore -----------------------------------------------------------


def test_store_save_and_load(tmp_path):
    store = WatchlistStore(tmp_path / "wl.sqlite")
    wl = Watchlist(
        date=date(2026, 5, 11),
        version=1,
        generated_at=now_utc(),
        reason="test",
        entries=(WatchlistEntry(symbol="A005930", score=1.0),),
    )
    store.save(wl)
    loaded = store.load_latest(date(2026, 5, 11))
    assert loaded is not None
    assert loaded.symbols == ("A005930",)
    assert loaded.entries[0].score == 1.0


def test_store_versioning(tmp_path):
    store = WatchlistStore(tmp_path / "wl.sqlite")
    target = date(2026, 5, 11)
    assert store.next_version_for(target) == 1
    wl_v1 = Watchlist(
        date=target, version=1, generated_at=now_utc(), reason="v1",
        entries=(WatchlistEntry(symbol="A"),),
    )
    store.save(wl_v1)
    assert store.next_version_for(target) == 2
    wl_v2 = Watchlist(
        date=target, version=2, generated_at=now_utc(), reason="v2",
        entries=(WatchlistEntry(symbol="B"),),
    )
    store.save(wl_v2)
    latest = store.load_latest(target)
    assert latest is not None
    assert latest.version == 2
    assert latest.symbols == ("B",)


def test_store_load_missing_returns_none(tmp_path):
    store = WatchlistStore(tmp_path / "wl.sqlite")
    assert store.load_latest(date(2026, 5, 11)) is None


def test_store_list_dates(tmp_path):
    store = WatchlistStore(tmp_path / "wl.sqlite")
    for d in [date(2026, 5, 9), date(2026, 5, 11), date(2026, 5, 10)]:
        store.save(
            Watchlist(date=d, version=1, generated_at=now_utc(), reason="t", entries=())
        )
    dates = store.list_dates()
    assert dates == [date(2026, 5, 11), date(2026, 5, 10), date(2026, 5, 9)]


# PreMarketWatchlistBuilder ------------------------------------------------


def test_builder_picks_top_n_by_trading_value(tmp_path):
    bar_store = BarStore(tmp_path / "bars")
    target = date(2026, 5, 11)
    yesterday = target - timedelta(days=1)
    bar_store.write(
        [
            _bar("A005930", yesterday, value=10_000_000_000),  # 100억
            _bar("A000660", yesterday, value=50_000_000_000),  # 500억 ← top
            _bar("A035720", yesterday, value=5_000_000_000),  # 50억
            _bar("A035420", yesterday, value=20_000_000_000),  # 200억 ← 2nd
        ]
    )
    wl_store = WatchlistStore(tmp_path / "wl.sqlite")
    builder = PreMarketWatchlistBuilder(bar_store=bar_store, watchlist_store=wl_store)
    config = BuildConfig(
        universe=("A005930", "A000660", "A035720", "A035420"), top_n=2
    )
    wl = builder.build(target_date=target, config=config)
    assert wl.symbols == ("A000660", "A035420")
    # Persisted
    loaded = wl_store.load_latest(target)
    assert loaded is not None
    assert loaded.symbols == wl.symbols


def test_builder_must_include_first_position(tmp_path):
    bar_store = BarStore(tmp_path / "bars")
    target = date(2026, 5, 11)
    yesterday = target - timedelta(days=1)
    bar_store.write(
        [
            _bar("BIG1", yesterday, value=100_000_000_000),
            _bar("SMALL", yesterday, value=1_000_000_000),
        ]
    )
    wl_store = WatchlistStore(tmp_path / "wl.sqlite")
    builder = PreMarketWatchlistBuilder(bar_store=bar_store, watchlist_store=wl_store)
    config = BuildConfig(
        universe=("BIG1", "SMALL"), top_n=1, must_include=("MUSTHAVE",)
    )
    wl = builder.build(target_date=target, config=config)
    assert wl.symbols[0] == "MUSTHAVE"
    assert "BIG1" in wl.symbols


def test_builder_must_exclude(tmp_path):
    bar_store = BarStore(tmp_path / "bars")
    target = date(2026, 5, 11)
    yesterday = target - timedelta(days=1)
    bar_store.write(
        [
            _bar("BIG1", yesterday, value=100_000_000_000),
            _bar("BANNED", yesterday, value=200_000_000_000),  # would be #1 but excluded
        ]
    )
    wl_store = WatchlistStore(tmp_path / "wl.sqlite")
    builder = PreMarketWatchlistBuilder(bar_store=bar_store, watchlist_store=wl_store)
    config = BuildConfig(
        universe=("BIG1", "BANNED"), top_n=2, must_exclude=("BANNED",)
    )
    wl = builder.build(target_date=target, config=config)
    assert "BANNED" not in wl.symbols
    assert "BIG1" in wl.symbols


def test_builder_skips_symbols_without_data(tmp_path):
    bar_store = BarStore(tmp_path / "bars")
    target = date(2026, 5, 11)
    yesterday = target - timedelta(days=1)
    bar_store.write([_bar("HASDATA", yesterday, value=10_000_000_000)])
    wl_store = WatchlistStore(tmp_path / "wl.sqlite")
    builder = PreMarketWatchlistBuilder(bar_store=bar_store, watchlist_store=wl_store)
    config = BuildConfig(universe=("HASDATA", "NODATA"), top_n=5)
    wl = builder.build(target_date=target, config=config)
    assert wl.symbols == ("HASDATA",)


def test_builder_uses_lookback_window(tmp_path):
    bar_store = BarStore(tmp_path / "bars")
    target = date(2026, 5, 11)
    # Bar from 7 days ago — outside default lookback of 5
    old = target - timedelta(days=7)
    bar_store.write([_bar("STALE", old, value=999_000_000_000)])
    wl_store = WatchlistStore(tmp_path / "wl.sqlite")
    builder = PreMarketWatchlistBuilder(bar_store=bar_store, watchlist_store=wl_store)
    config = BuildConfig(universe=("STALE",), top_n=5, lookback_days=5)
    wl = builder.build(target_date=target, config=config)
    assert wl.symbols == ()


def test_builder_rejects_empty_universe(tmp_path):
    bar_store = BarStore(tmp_path / "bars")
    wl_store = WatchlistStore(tmp_path / "wl.sqlite")
    builder = PreMarketWatchlistBuilder(bar_store=bar_store, watchlist_store=wl_store)
    with pytest.raises(ValueError, match="universe"):
        builder.build(target_date=date(2026, 5, 11), config=BuildConfig(universe=()))


# Helpers ------------------------------------------------------------------


def test_universe_from_bar_store(tmp_path):
    bar_store = BarStore(tmp_path / "bars")
    bar_store.write(
        [
            _bar("A005930", date(2026, 5, 10), value=1_000),
            _bar("A000660", date(2026, 5, 10), value=2_000),
        ]
    )
    universe = universe_from_bar_store(bar_store)
    assert set(universe) == {"A005930", "A000660"}


def test_aggregate_by_theme():
    entries = (
        WatchlistEntry(symbol="A005930"),
        WatchlistEntry(symbol="A000660"),
        WatchlistEntry(symbol="A035720"),
    )
    themes = aggregate_by_theme(
        entries, theme_of={"A005930": "semiconductor", "A000660": "semiconductor"}
    )
    assert themes == {"semiconductor": ["A005930", "A000660"]}
