"""Tests for ThemePairBuilder."""

from datetime import UTC, date, datetime

import pytest

from ks_ws.storage.watchlist import Watchlist, WatchlistEntry
from ks_ws.strategies.pair_mapping import ThemePairBuilder


def _wl(symbols: list[str]):
    return Watchlist(
        date=date(2026, 5, 11),
        version=1,
        generated_at=datetime.now(UTC),
        reason="test",
        entries=tuple(WatchlistEntry(symbol=s) for s in symbols),
    )


def test_builds_pair_from_top_two_by_market_cap():
    builder = ThemePairBuilder(
        theme_of={"A005930": "semi", "A000660": "semi"},
        market_cap_of={"A005930": 500_000_000_000_000, "A000660": 100_000_000_000_000},
    )
    pairs = builder.build_pairs(_wl(["A005930", "A000660"]))
    assert pairs == {"A005930": "A000660"}  # samsung leader, sk hynix follower


def test_skips_themes_with_single_symbol():
    builder = ThemePairBuilder(
        theme_of={"X": "alpha"},
        market_cap_of={"X": 1000},
    )
    assert builder.build_pairs(_wl(["X"])) == {}


def test_drops_symbols_missing_theme_or_cap():
    builder = ThemePairBuilder(
        theme_of={"A": "t1"},  # B missing
        market_cap_of={"A": 100, "B": 50},
    )
    assert builder.build_pairs(_wl(["A", "B"])) == {}  # only 1 valid → no pair


def test_multiple_themes_independent():
    builder = ThemePairBuilder(
        theme_of={
            "A": "semi", "B": "semi",
            "C": "battery", "D": "battery",
        },
        market_cap_of={"A": 200, "B": 100, "C": 80, "D": 50},
    )
    pairs = builder.build_pairs(_wl(["A", "B", "C", "D"]))
    assert pairs == {"A": "B", "C": "D"}


def test_picks_top_two_when_more_available():
    """3 symbols in same theme → only top 2 paired (1등→2등)."""
    builder = ThemePairBuilder(
        theme_of={"A": "t", "B": "t", "C": "t"},
        market_cap_of={"A": 100, "B": 80, "C": 50},
    )
    pairs = builder.build_pairs(_wl(["A", "B", "C"]))
    assert pairs == {"A": "B"}


def test_accepts_iterable_of_entries():
    builder = ThemePairBuilder(
        theme_of={"A": "t", "B": "t"},
        market_cap_of={"A": 100, "B": 80},
    )
    entries = [WatchlistEntry(symbol="A"), WatchlistEntry(symbol="B")]
    pairs = builder.build_pairs(entries)
    assert pairs == {"A": "B"}


def test_multi_followers():
    builder = ThemePairBuilder(
        theme_of={"A": "t", "B": "t", "C": "t", "D": "t"},
        market_cap_of={"A": 1000, "B": 500, "C": 300, "D": 100},
    )
    out = builder.build_multi_followers(_wl(["A", "B", "C", "D"]), max_followers=2)
    assert out == {"A": ["B", "C"]}


def test_multi_followers_validation():
    builder = ThemePairBuilder(theme_of={}, market_cap_of={})
    with pytest.raises(ValueError):
        builder.build_multi_followers(_wl([]), max_followers=0)
