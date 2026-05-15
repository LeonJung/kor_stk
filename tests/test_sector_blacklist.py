"""Tests for SectorBlacklist (Tier 1)."""

from __future__ import annotations

from ks_ws.sources.sector_blacklist import (
    BLACKLIST_NAMES,
    filter_universe,
    is_blacklisted,
)


def test_exact_match_blacklisted():
    nm = {"005930": "삼성전자", "259960": "크래프톤"}
    assert is_blacklisted("259960", nm) is True
    assert is_blacklisted("005930", nm) is False


def test_substring_match():
    nm = {"X1": "카카오게임즈"}  # contains '카카오'
    assert is_blacklisted("X1", nm) is True


def test_too_short_keyword_no_match():
    # 'NC' 매칭 — 짧지만 BLACKLIST_NAMES 에 있는 exact + 길이 < 3 substring 무시
    nm = {"X1": "NC", "X2": "NCsoft", "X3": "NC다이노스"}
    # 'NC' 정확 매칭 = blacklisted
    assert is_blacklisted("X1", nm) is True
    # substring 매칭은 키워드 길이 ≥ 3 에서만 — 'NC' (2자) 는 sub 매칭 X
    assert is_blacklisted("X2", nm) is False
    assert is_blacklisted("X3", nm) is False


def test_unknown_symbol_not_blacklisted():
    nm = {}
    assert is_blacklisted("999999", nm) is False


def test_filter_universe():
    nm = {"005930": "삼성전자", "259960": "크래프톤",
          "000660": "SK하이닉스", "035420": "NAVER"}
    codes = list(nm.keys())
    filtered = filter_universe(codes, nm)
    assert "005930" in filtered
    assert "000660" in filtered
    assert "259960" not in filtered  # 크래프톤
    assert "035420" not in filtered  # NAVER


def test_blacklist_set_not_empty():
    assert len(BLACKLIST_NAMES) > 30  # 충분히 큼
    assert "크래프톤" in BLACKLIST_NAMES
    assert "NAVER" in BLACKLIST_NAMES
