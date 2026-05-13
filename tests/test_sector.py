"""SectorClassifier — GICS 매핑."""
from __future__ import annotations

import pytest

from ks_ws.sources.sector import (
    DEFAULT_KOSPI_TOP30_GICS,
    GICS_SECTORS,
    SectorClassifier,
)


def test_default_loaded() -> None:
    clf = SectorClassifier()
    # 삼성전자 / 하이닉스 / 카카오 같은 익숙한 매핑
    assert clf.classify("005930") == "Information Technology"
    assert clf.classify("000660") == "Information Technology"
    assert clf.classify("035720") == "Communication Services"
    assert clf.classify("005380") == "Consumer Discretionary"
    assert clf.classify("207940") == "Health Care"


def test_unknown_symbol() -> None:
    clf = SectorClassifier()
    assert clf.classify("999999") == "unknown"


def test_runtime_extension() -> None:
    clf = SectorClassifier()
    clf.set_mapping("999999", "Industrials")
    assert clf.classify("999999") == "Industrials"


def test_set_mapping_unknown_sector() -> None:
    clf = SectorClassifier()
    with pytest.raises(ValueError):
        clf.set_mapping("999999", "BadSector")


def test_same_sector_match() -> None:
    clf = SectorClassifier()
    # 005930 + 000660 same IT sector
    assert clf.same_sector("005930", "000660")


def test_same_sector_mismatch() -> None:
    clf = SectorClassifier()
    # 005930 IT vs 005380 Consumer Discretionary
    assert not clf.same_sector("005930", "005380")


def test_same_sector_unknown_returns_false() -> None:
    clf = SectorClassifier()
    assert not clf.same_sector("005930", "999999")


def test_sector_members() -> None:
    clf = SectorClassifier()
    it_members = clf.sector_members("Information Technology")
    assert "005930" in it_members
    assert "000660" in it_members
    assert sorted(it_members) == it_members  # sorted by symbol


def test_sector_members_invalid() -> None:
    clf = SectorClassifier()
    with pytest.raises(ValueError):
        clf.sector_members("BadSector")


def test_all_sectors_with_members() -> None:
    clf = SectorClassifier()
    all_map = clf.all_sectors_with_members()
    assert "Information Technology" in all_map
    assert "Communication Services" in all_map
    # 모든 GICS sector 가 있을 필요 X (utilities/RE 부족 가능)
    for sec in all_map:
        assert sec in GICS_SECTORS


def test_invalid_mapping_construction() -> None:
    with pytest.raises(ValueError):
        SectorClassifier({"005930": "BadSector"})


def test_len_and_contains() -> None:
    clf = SectorClassifier()
    assert len(clf) >= 30  # default 30+ 종목
    assert "005930" in clf
    assert "999999" not in clf


def test_default_kospi_30_coverage() -> None:
    """Default 매핑이 시총 상위 ~30+ 종목 cover 하는지 + GICS 11 sector 중 5+ sector 갖춤."""
    sectors_present = {sec for sec in DEFAULT_KOSPI_TOP30_GICS.values()}
    assert len(sectors_present) >= 5
    assert len(DEFAULT_KOSPI_TOP30_GICS) >= 30
