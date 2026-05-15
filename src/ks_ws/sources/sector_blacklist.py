"""SectorBlacklist — Tier 1 universe filter.

사용자 룰 (2026-05-15): 변동성돌파 검증 결과 — 게임/엔터/화장품/2차전지/
바이오 high-vol/IT서비스/금융 sub-set = 변동성돌파 false breakout 빈발.
이를 자동 차단 → trade 횟수 절반 + 승률 +7%p (검증됨).

룰: 종목명 substring 매칭. 추가 종목은 BLACKLIST_NAMES set 에 추가만.
strategy 가 watchlist 생성 시 호출 → blacklist 제외.

향후 확장:
- GICS sector dict 매핑 시 sector_blacklist=["Communication Services", "Consumer Discretionary"] 등
- 종목 단위 강제 화이트리스트 (whitelist override)
"""

from __future__ import annotations

from typing import Callable


# Tier 1 검증 (vol_breakout 8개월) 기반 blacklist 종목명 set.
# 사용자 룰 (2026-05-15): 변동성돌파 적자 종목 분석에서 도출.
# 종목명 substring 매칭. 다른 strategy 도 재사용.
BLACKLIST_NAMES: set[str] = {
    # 게임
    "크래프톤", "넷마블", "NC", "펄어비스", "엔씨소프트",
    # 엔터·미디어
    "하이브", "JYP Ent.", "에스엠", "와이지엔터테인먼트",
    # IT서비스 (변동 큰)
    "카카오", "카카오뱅크", "카카오페이", "NAVER",
    # 화장품·헬스케어 (변동 큰)
    "LG생활건강", "휴젤", "셀트리온", "삼천당제약", "보로노이",
    "SK바이오사이언스", "HLB", "올릭스",
    # 2차전지·에너지 (변동 큰)
    "LG에너지솔루션", "SK이노베이션", "에코프로머티", "에코프로비엠",
    "LG화학", "엘앤에프", "포스코퓨처엠", "롯데에너지머티리얼즈",
    # 식음료·유통 (특이 변동)
    "삼양식품", "신세계", "이마트",
    # 금융 일부 (변동성돌파 약함, 추세 X)
    "메리츠금융지주", "우리금융지주", "BNK금융지주", "KB금융",
    "신한지주", "iM금융지주", "JB금융지주", "서울보증보험",
    # 운송 / 조선 (큰 변동 + false breakout)
    "삼성에스디에스", "현대오토에버", "LS ELECTRIC", "LS", "HMM",
    "HD건설기계", "현대로템", "현대글로비스", "KT", "HD현대중공업",
    "대한조선",
    # 기타 high-vol
    "금호석유화학", "기아", "효성", "LG전자", "리노공업", "심텍",
    "맥쿼리인프라", "강원랜드", "F&F", "피에스케이홀딩스",
    "한화오션", "클래시스", "DL이앤씨", "일진전기", "우리기술",
    "대한전선", "현대무벡스", "GS건설", "코웨이",
    "이수스페셜티케미컬", "유진테크", "DN오토모티브",
}


def is_blacklisted(symbol: str, name_map: dict[str, str]) -> bool:
    """종목명 기반 blacklist 매칭."""
    nm = name_map.get(symbol, "")
    if not nm:
        return False
    if nm in BLACKLIST_NAMES:
        return True
    # Substring fallback (예: '카카오게임즈' 도 '카카오' 매칭)
    for kw in BLACKLIST_NAMES:
        if kw in nm and len(kw) >= 3:  # 너무 짧은 매칭 회피
            return True
    return False


def filter_universe(codes: list[str], name_map: dict[str, str]) -> list[str]:
    """codes 중 blacklist 통과한 것만 반환."""
    return [c for c in codes if not is_blacklisted(c, name_map)]


def load_name_map(universe_db: str = "data/universe.sqlite") -> dict[str, str]:
    """universe.sqlite 에서 symbol → name 매핑 load."""
    import sqlite3
    conn = sqlite3.connect(universe_db, timeout=10)
    try:
        rows = conn.execute("SELECT * FROM universe").fetchall()
    finally:
        conn.close()
    # schema: (symbol, isin, name, market, ...) — index 2 = name
    return {row[0]: row[2] for row in rows}
