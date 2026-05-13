# 19 strategy 데이터 의존성 + 검증 가능성

> 2026-05-13 작성. paper_trade 의 19 strategy 가 어떤 데이터를 쓰는지 + backtest
> 가능 여부.

## 데이터 layer 정리

| Layer | Source | Coverage (5/13 기준) |
|---|---|---|
| `1d` (일봉) | BarStore `data/bars/1d/` | 696 bars × 20+ sym, **2023-08 ~ 2026-05-11** |
| `1m` (분봉) | BarStore `data/bars/1m/` | 190K bars × 20+ sym, **2024-04 ~ 2026-05-11** |
| Tick | `data/ticks.sqlite` ticks table | **6.07M tick, 2026-05-11 ~ 5/13** (3일) |
| OrderBook (호가) | `data/ticks.sqlite` orderbook | **0** (KIS mock H0STASP0 미지원) |
| Foreign flow | KIS REST `investor-trade-by-stock-daily` 일별 | API 호출 시점 (live polling) |
| Market investor flow | KIS REST `inquire-investor-time-by-market` 분봉 | API 호출 시점 (60s polling) |
| Valuation (PER/PBR) | KIS REST `inquire-price` | API 호출 시점 (1회) |
| KOSPI index | BarStore `KOSPI` 1d | 58 bars (recently added) |

## 19 strategy × 데이터 의존성

| # | Strategy | 한국어 | 일봉 (1d) | 분봉 (1m) | Tick | Foreign event | Other |
|---|---|---|---|---|---|---|---|
| 1 | breakout | 신고가매매 | ✓ 60일 high | — | ✓ | — | — |
| 2 | closing_bet | 종가베팅 | — | — | ✓ | — | DojiCandle event |
| 3 | double_bottom | 쌍바닥매매 | ✓ detector | — | (exit only) | — | DoubleBottomDetected event |
| 4 | box_breakout | 박스권돌파매매 | ✓ detector | — | (exit only) | — | BoxBreakoutDetected |
| 5 | inverse_head_shoulders | 역헤드앤숄더매매 | ✓ detector | — | (exit only) | — | HeadShouldersDetected |
| 6 | flag_pennant | 깃발페넌트매매 | ✓ detector | — | (exit only) | — | FlagPennantDetected |
| 7 | cup_handle | 컵앤핸들매매 | ✓ detector | — | (exit only) | — | CupHandleDetected |
| 8 | triangle | 삼각수렴매매 | ✓ detector | — | (exit only) | — | TriangleDetected |
| 9 | wedge | 웨지매매 | ✓ detector | — | (exit only) | — | WedgeDetected |
| 10 | volatility_breakout | 변동성돌파 | ✓ 전일 H/L | — | ✓ open + cross | — | — |
| 11 | vwap_reversion | VWAP평균회귀 | — | — | ✓ 누적 VWAP + σ | — | — |
| 12 | nr7_breakout | NR7돌파 | ✓ 7일 range | — | ✓ prev_high cross | — | — |
| 13 | bnf_disparity | BNF이격도 | — | ✓ MA25 | ✓ | — | — |
| 14 | dual_thrust | 듀얼트러스트 | ✓ 5일 range | — | ✓ open + cross | — | — |
| 15 | opening_momentum | 시초모멘텀 | — | — | ✓ 09:03-09:25 surge | — | — |
| 16 | foreign_flow | 외국인수급 | — | — | ✓ entry | ✓ trigger | ForeignNetBuy event |
| 17 | color_streak | 양봉연속 | ✓ 양봉 streak | — | ✓ prev_close cross | — | — |
| 18 | pivot_half_pullback | 피벗절반눌림 | ✓ pivot levels | — | ✓ R1 touch + half cross | — | — |
| 19 | tape_burst | 체결폭주 | — | — | ✓ 분당 카운트 burst | — | — |

## Backtest 가능성

데이터 layer 별 가능한 strategy:

### A. 일봉만 충분 (5 strategies — 가장 backtest 친화적)
- double_bottom, box_breakout, inverse_head_shoulders, flag_pennant, cup_handle, triangle, wedge
- 696일 × 20 sym → 매우 풍족. 2-3년 backtest 가능.

### B. 일봉 + 분봉 (1 strategy)
- bnf_disparity (1m MA25)
- 1년치 분봉 데이터 있음 → 1년 backtest 가능.

### C. 일봉 setup + tick entry (8 strategies)
- breakout, volatility_breakout, nr7_breakout, dual_thrust, color_streak, pivot_half_pullback
- 일봉 setup 은 2-3년 있지만 tick 은 **3일치 (5/11-13)** 뿐.
- → tick backtest 는 3일만 가능. 분봉 OHLC 로 cross 시점 추정하면 1년 backtest 가능 (정확도 ↓).

### D. Tick-only (3 strategies — 가장 제한적)
- vwap_reversion, opening_momentum, tape_burst, closing_bet (DojiCandle 분봉)
- 3일치만 backtest 가능 — 5/11-13.
- closing_bet 은 DojiEmitter 가 13:30 후 partial OHLC 검사 → 일봉으로 근사 가능.

### E. External event (1 strategy)
- foreign_flow (ForeignNetBuy event)
- 과거 ForeignNetBuy event 가 없음 (live 만). 일별 외인 매수 데이터로 합성 event 생성하여 backtest 가능 (1d daily flow).

## 결론 — 3-tier backtest 전략

1. **Tier 1: 일봉 backtest** (1-3년) — 13 strategies (A + B + C tier 의 setup 단)
2. **Tier 2: 분봉 backtest** (1년) — bnf_disparity + C tier 의 분봉 시뮬레이션
3. **Tier 3: tick backtest** (3일) — D tier 3 strategies + C tier 정확 검증

먼저 Tier 1 (가장 풍족) + Tier 3 (3일치 = 가장 현실적) 부터 진행.
