# Strategy 투자스타일 그룹화 + 매매기준 재조정

> 2026-05-14 사용자 명시:
> - technical = 단타/스캘핑, fundamental = 중기/장기 (보는 데이터 다름)
> - 일봉 보면서 단타/스캘핑 X, 분봉 보면서 중장투 X (손절 타이트)
> - **fundamental 은 단타/스윙에도 들어감** (거래대금 history, 종목군 = fundamental layer)
> - 5 그룹 (스캘핑/단타/스윙/중기/장기), 장기는 LLM 필요 → TODO
>
> **이 doc 의 markdown 표 = 사용자 대화 포맷 표준**. 앞으로 strategy 관련 토론은 이 형식 그대로.

핵심 원칙: **strategy 의 데이터 단위(input) 와 hold 시간(output) 이 일치해야 함**.
일봉 패턴인데 4시간 hold = 시스템적 오류 (현재 paper_trade 의 8 strategies).

---

## TP/SL 결정 룰 (사용자 명시 2026-05-14)

**(B) Dynamic — ATR 기반 strategy 자동 결정**:
- 각 strategy 가 entry 시점 ATR (변동성) 기준으로 TP/SL 자동 계산
- 종목별 + 시점별로 적정 TP/SL 가 달라짐 (변동성 큰 종목 = 넓은 TP/SL)
- hard-coded value 폐기

**ATR 계산 단위 = 스타일의 데이터 주기와 동일**:
| 스타일 | ATR 계산 주기 | ATR period |
|---|---|---|
| 스캘핑 | 1분봉 ATR | 14 |
| 단타 | 5분봉 ATR | 14 |
| 스윙 | 15분봉 ATR | 14 |
| 중기 | 일봉 ATR | 14 |
| 장기 | 일봉/주봉 ATR | 14 |

**ATR multiplier 권고 (스타일별)**:
| 스타일 | TP × ATR | SL × ATR | 이유 |
|---|---|---|---|
| 스캘핑 | **1.0** | **0.5** | 작은 hold, 빠른 익절 |
| 단타 | **2.0** | **1.0** | 당일 hold, 중간 익절 |
| 스윙 | **4.0** | **2.0** | 며칠 hold, 큰 익절 |
| 중기 | **8.0** | **3.0** | 몇 주-몇 달, 큰 변동 허용 |
| 장기 | (별도) | (별도) | 100% trailing 룰 (위 참조) |

예시 (스윙, 삼성전자 ATR 5,000원, entry 285,000원):
- TP = entry + 4 × ATR = 285,000 + 20,000 = 305,000원 (+7%)
- SL = entry − 2 × ATR = 285,000 − 10,000 = 275,000원 (-3.5%)
- 종목 변동성 따라 자동 — 변동성 큰 LG에너지 (ATR 12,000) 이면 TP +17%, SL -8.4%

**Fallback**:
- ATR 데이터 없을 시 (분봉 ATR 없음 등) hard-coded fallback 사용 (v3 의 단일 값)
- backtest 시점 = ATR 계산 가능 (historical data 충분)
- live 시작 시점 = 종목별 ATR pre-compute (paper_trade startup)

## 5 투자스타일 정의

| 스타일 | hold | 데이터 | TP/SL | Fundamental 보조 | 외인/기관 수급 주기 |
|---|---|---|---|---|---|
| 🟢 스캘핑 | **≤15분** | 틱 / 1분봉 / 호가 | TP 0.5-1.5% / SL 0.3-1% | 거래대금/호가 잔량 | **종목 실시간** (Daishin CYBOS Plus 종목 외인/기관 push, TODO 5월) |
| 🟡 단타 | 30분-당일 | 분봉 + 일봉 setup | TP 1.5-4% / SL 1-2.5% | 거래대금 history | **분봉/시간 단위**: KIS `inquire-investor-time-by-market` 60s (시장 합산, 적용중) + Daishin 종목 분봉 (TODO) |
| 🔵 스윙 | 2일-2주 | **15분봉** + 일봉 | TP 5-15% / SL 3-7% | 종목군(sector), 거래대금 추세 | **15분/시간 누적**: 위 + 15분봉 aggregation |
| 🟣 중기 | **2주-6개월** | 일봉/주봉 + fundamental | TP 15-30% / SL 7-12% | PER/PBR, 외인 추세, 섹터 로테이션 | **일봉**: KIS `investor-trade-by-stock-daily` 일별 종목 단위 (적용중) |
| ⚪ 장기 | **6개월+** (TODO) | 일봉/주봉 + fundamental + **LLM** | **TP 100%+ trailing -20% / SL 20%** | 공시/뉴스 NLP, 재무제표, 산업 전망 | **주봉/월봉 누적**: 일별 합산 + 분기 외인 비중 추적 |

**Fundamental layer 적용**:
- 모든 그룹의 BUY signal 에 `FundamentalAllocator.macro_score` 곱 (이미 적용 중)
- 스캘핑/단타 = macro_score 가 보조 필터 (강도)
- 스윙/중기/장기 = macro_score 가 핵심 결정 (방향성 + 종목 선별)

**외인/기관/개인 3 주체 수급 데이터 = 모든 스타일에 필수**:
- 사용자 명시 2026-05-14: "외국인수급, 기관수급 확인은 장투 뿐만 아니라
  스캘핑, 단타, 스윙, 중기에서도 데이터 확인해줘야 됨. 데이터 확인 주기는 각
  매매 스타일이 참조하는 데이터의 주기와 최대한 같아야 함. 실시간으로 받을
  수 있으면 최고."
- 현재 적용:
  - 시장 단위 분봉 (KOSPI/KOSDAQ 합산) = `RealtimeInvestorFlowSource` 60s polling
    (외인/기관/**개인** 3 주체 모두 KIS API 응답에 포함됨)
  - 종목 단위 일별 = `kis_foreign_flow_fetcher` (외인만, paper_trade 5일 trend)
- 미적용 (TODO):
  - 종목 단위 실시간/분봉 = Daishin CYBOS Plus push (cycle 5 doc)
  - 종목 단위 15분봉 누적 = 분봉 → 15분 aggregation
  - 종목 단위 기관/개인 일별 데이터 fetch (현재 외인만 추적)

**2026 KOSPI 새 패턴 (사용자 명시 2026-05-14)**:
- 과거 (~2025): "외인 사면 무조건 오른다"
- **2026**: KOSPI 는 **개인 + 기관**이 사줘야 "더 많이" 오름. 외인 단독 약함.
- 적용 변경:
  - `score_from_market_flow` 의 smart_money = foreign + institution 룰
    → `combined = institution + individual + foreign * 0.5` 같은 가중 변경
  - `ForeignFlowStrategy` (외국인수급) trigger 강화: 외인 단독 X → 외인 +
    (기관 OR 개인) 동조 시만 매수
  - **예외**: 일중 단타/스캘핑 에서는 외인 단독 매수 = 짧은 모멘텀 trigger 유효
  - 중기/장기 매매에서만 새 패턴 (개인+기관 우선) 강력 적용

**3 주체 별 수급 weight 권고 (2026 패턴 반영)**:
| 스타일 | 외인 | 기관 | 개인 | 합산 룰 |
|---|---|---|---|---|
| 스캘핑 | 1.0 | 1.0 | 0.5 | 외인 단독 spike 도 OK (단기 momentum) |
| 단타 | 0.7 | 1.0 | 0.7 | 외인 + 기관/개인 동조 시 강화 |
| 스윙 | 0.5 | 1.0 | 1.0 | **개인+기관 우선**, 외인 보조 |
| 중기 | 0.4 | 1.2 | 1.0 | 개인+기관 핵심, 외인 X 도 가능 |
| 장기 | 0.3 | 1.0 | 1.2 | 개인 누적 매수 > 외인 |

---

## 19 Strategies 분류

### 🟢 스캘핑 (3개) — ≤15분
| Strategy | 한국어 | 입력 | hold | TP/SL |
|---|---|---|---|---|
| `opening_momentum` | 시초모멘텀 | 09:00 시초 + tick | 15min | TP 1.5% / SL 0.8% |
| `tape_burst` | 체결폭주 | 분봉 tick 카운트 | 15min | TP 1.0% / SL 0.5% |
| `vwap_reversion` | VWAP평균회귀 | tick + 분봉 σ | 15min | TP 1.0% / SL 0.6% |

### 🟡 단타 (7개) — 30분-당일
| Strategy | 한국어 | 입력 | hold | TP/SL |
|---|---|---|---|---|
| `breakout` | 신고가매매 | 60일 high + tick | 240min (당일) | TP 2.0% / SL 1.5% |
| `volatility_breakout` | 변동성돌파 | 전일 H/L + tick | 240min | TP 2.5% / SL 1.5% |
| `nr7_breakout` | NR7돌파 | 7일봉 range + tick | 240min | TP 2.5% / SL 1.5% |
| `bnf_disparity` | BNF이격도 | 분봉 MA25 | 240min | TP 3.0% / SL 2.0% |
| `dual_thrust` | 듀얼트러스트 | 5일봉 range + tick | 240min | TP 2.5% / SL 1.5% |
| `pivot_half_pullback` | 피벗절반눌림 | 일봉 pivot + tick | 240min | TP 2.5% / SL 1.5% |
| `closing_bet` | 종가베팅 | 13:30 분봉 도지 + overnight | overnight | TP 2.0% / SL 2.0% |

### 🔵 스윙 (8개) — 2일-2주, **15분봉 + 일봉**
| Strategy | 한국어 | 입력 | hold | TP/SL |
|---|---|---|---|---|
| `double_bottom` | 쌍바닥매매 | 일봉 W + 15분봉 entry | 5일 | TP 8% / SL 4% |
| `box_breakout` | 박스권돌파매매 | 일봉 N일 box + 15분봉 | 5일 | TP 7% / SL 4% |
| `inverse_head_shoulders` | 역헤드앤숄더매매 | 일봉 H&S + 15분봉 | 7일 | TP 10% / SL 5% |
| `flag_pennant` | 깃발페넌트매매 | 일봉 깃발 + 15분봉 | 3일 | TP 7% / SL 4% |
| `cup_handle` | 컵앤핸들매매 | 일봉 컵 + 15분봉 | 10일 | TP 12% / SL 6% |
| `triangle` | 삼각수렴매매 | 일봉 삼각 + 15분봉 | 5일 | TP 8% / SL 4% |
| `wedge` | 웨지매매 | 일봉 wedge + 15분봉 | 5일 | TP 8% / SL 4% |
| `color_streak` | 양봉연속 | 일봉 N양봉 + 15분봉 | 3일 | TP 6% / SL 3% |

### 🟣 중기 (1개) — 2주-6개월, 일봉/주봉 + fundamental
| Strategy | 한국어 | 입력 | hold | TP/SL |
|---|---|---|---|---|
| `foreign_flow` | 외국인수급 | ForeignNetBuy event + 일별 누적 + PER/PBR | 20-90일 | TP 20% / SL 8% |

### ⚪ 장기 (0개, TODO) — 6개월+, LLM 필요
현재 없음. 추후 추가:
- 공시/뉴스 NLP 기반 (산업 변화, 정책 영향)
- 재무제표 변화 추적 (분기 실적)
- 산업 전망 (LLM 분석)

**장기 청산 룰 (사용자 명시 2026-05-14)**:
- **단계별 trailing anchor**: 100% / 200% / 300% / 400% / ... 도달 시 그 단계가 anchor
- 통과한 **최고 단계 anchor 기준 -20%** 떨어지면 익절
- 도달 후 -20% 안 떨어지면 hold (다음 단계 노려)
- 더 높은 단계 도달 시 anchor 갱신 (락업 ratchet)
- SL: 100% 도달 전 (= 아직 anchor 활성 X) entry -20% 도달 시 손절
- 의사코드:
  ```
  current_n = int(tick.price / entry)  # 1.5x → 1, 2.3x → 2, 3.0x → 3
  max_anchor_n = max(max_anchor_n, current_n)
  if max_anchor_n >= 2:  # 100% (= entry*2) 도달 이력
      anchor_price = entry * max_anchor_n
      if tick.price <= anchor_price * 0.8:  # anchor -20%
          SELL  # 익절
  elif tick.price <= entry * 0.8:  # 아직 anchor X, entry -20%
      SELL  # 손절
  ```

예시 (entry = 100원):
- 가격 180원 (80% 수익) — anchor 활성 X, SL = 80 (entry × 0.8), hold
- 가격 220원 (120%) → max_anchor_n = 2, anchor = 200, trailing = 160
- 가격 280원 (180%) → anchor 그대로 200 (n=2 still), trailing = 160
- 가격 320원 (220%) → max_anchor_n = 3, anchor = 300, trailing = 240
- 가격 245원으로 하락 → 240 위, hold
- 가격 235원 → trailing 240 이탈 → SELL 익절

---

## 핵심 진단

### ❌ 현재 시스템 오류 (9개 strategies)
- **일봉 패턴 8개** + **외국인수급 1개** = 240-360분 hold + TP 3%/SL 2%
- 일봉 변동성 1-5% 범위 안에서 SL 먼저 trigger → 거의 매번 손실
- 4시간 hold 안에 패턴 완성/매수 추세 발현 거의 불가능
- 외국인수급 = 외인 매수 추세는 보통 몇 주-몇 개월 (단기 X)
- = 사용자 지적의 "**손절 타이트해서 손실만**" 정확히 일치

### ✅ 올바른 매핑 결과
- 스캘핑 (3) = 분/초 분해능, hold ≤15분
- 단타 (7) = 당일 청산, hold 30분-1일
- 스윙 (8) = **15분봉 + 일봉**, hold 2일-2주
- 중기 (1) = 일봉/주봉 + fundamental, hold 2주-6개월
- 장기 (0, TODO) = LLM 필요, hold 6개월+

### Fundamental 데이터 활용 (사용자 명시)
모든 그룹에 보조 layer 로:
- 거래대금 history (`bar.value` 누적) — 스캘핑/단타 entry 강도
- 종목군 (sector) — 스윙 (sector rotation), 중기 (산업 트렌드)
- 외인 일별 — 단타/스윙 entry 컨펌
- PER/PBR/EPS — 중기/장기 종목 선별
- macro_score = 이 모든 것의 blend (현재 paper_trade 적용 중)

---

## 적용 계획 (사용자 검토 후)

### Phase A: 코드 수정 (스윙 strategies 우선) — ✅ 2026-05-15 완료
1. ✅ 패턴 strategies hold 5-10일 + ATR swing multiplier (atr_swing 주입)
2. ✅ 패턴 strategies fallback TP/SL 8%/4% (ATR 우선, fallback)
3. ✅ 외국인수급 hold 30일 + TP 20% / SL 8% + atr_mid 주입 (style="mid_term")
4. ✅ 스캘핑 strategies (vwap/opening/tape_burst) atr_scalping 주입 + style="scalping"
5. ⏳ opening_momentum force_exit 09:50 → 09:25 (50분 → 15분) — 후속

### Phase A.5: ATR 동적 TP/SL 전체 적용 — ✅ 2026-05-15 완료
- 모든 19 strategies 가 atr_provider 받음 (None fallback = hard-coded pct)
- ATR 우선, ATR=0 또는 unavailable 시 fallback pct 사용
- BNF=day_trade (doc 분류대로), 6 단타 + 8 스윙 + 1 중기 + 3 스캘핑
- paper_trade_breakout.py 가 4 ATR providers (1m/5m/15m/1d) 구성 후 strategy 별 주입

### Phase A.6: 장기 trailing framework — ✅ 2026-05-15 완료
- `_long_term_trailing.LongTermTrailingState` 단계별 anchor ratchet 구현
- 100%/200%/.../ N×entry 도달 = anchor 갱신, anchor -20% trailing
- anchor 미활성 시 entry -20% SL
- 장기 strategies (TODO, LLM 필요) 가 사용. 8 테스트 통과.

### Phase B: 15분봉 데이터 확보
- 현재 BarStore 의 1m 만 보유 (190K bars/sym × 1년)
- 15분봉 = 1m × 15 aggregation (실시간 생성 가능)
- 또는 Daishin sync 로 15m 별도 fetch

### Phase C: ledger overnight hydrate
- 현재 closing_bet 만 hydrate
- 스윙 strategies (8개) 도 hydrate → 다음날 paper_trade 재시작 시 hold 지속

### Phase D: 검증
- 스윙 strategies = 5-10일 hold backtest (일봉)
- 스캘핑 = 분봉 backtest (15min hold)
- reliability_validation 재실행

---

## 사용자 룰 정렬

- **사용자 룰 7 (technical/fundamental 분리)**: 진입/청산 timing = technical, 종목분석/수급/시장 = fundamental
- **이번 스타일 그룹화**: 매매 시간 단위로 분류 (스캘핑/단타/스윙/중기/장기)
- 같은 strategy 는 (technical) + (스타일) 2D 매트릭스 위치 보유
- Fundamental 은 모든 스타일에 보조 layer (macro_score)
