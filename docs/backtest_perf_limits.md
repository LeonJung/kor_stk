# Backtest perf 한계 분석 + 향후 plan

> 2026-05-14 작성. cycle 22-30 backtest 실행 중 발견된 perf 한계.

## 현재 실용 max

| Mode | 데이터 | items | 메모리 | 시간 |
|---|---|---|---|---|
| 일봉 696일 × 20 종목 | 14K bars | 30K items | ~50MB | 5.7s |
| 분봉 30일 × 20 종목 | 137K bars | 274K items | ~150MB | 7.7s |
| 분봉 90일 × 20 종목 | 424K bars | 849K items | ~500MB | 20.3s |
| 분봉 180일 × 20 종목 | 886K bars | 1.77M items | ~1.5GB | 44.9s |
| 분봉 400일 × 20 종목 | 2M bars | 4M items | **4GB+** | **swap → 중단** |
| 분봉 400일 × 10 종목 | 1M bars | 2M items | ~2GB | 60s |

## Bottleneck

1. **pydantic Bar/Tick instance 메모리** — 각 ~200 byte
   - 4M items × 200B = 800MB pure data
   - 추가로 dict/list overhead 1-2x
2. **TickReplayDriver.run() 의 `sorted(self.items)`** — 4M items 정렬 = ~250MB temp memory
3. **Detector pre-feed (cycle 시작 시 일괄)** — 분봉 패턴은 의미 X → 이미 skip 옵션 (--with-patterns)

## 가능한 개선 방향

### A. Streaming generator (메모리 1/n 까지)
- `sorted(items)` → `heapq.merge(per_sym_iterators)` (각 sym sorted 가정)
- `items list` 안 만들고 yield
- 변경 영향: TickReplayDriver.run() 가 generator 받게 변경
- 예상 효과: 메모리 70-80% 절감, 1년치 가능

### B. 데이터 modeling 가볍게
- pydantic Bar/Tick → namedtuple 또는 slot-class
- 메모리 절반 ↓
- 변경 영향: domain.py 전체 + 모든 strategy 호환 필요
- 위험: live 코드 path 호환성 손상 가능

### C. Chunked backtest (월 단위 등)
- 1년 → 12 chunk 로 나눠 별도 실행, 결과 sum
- 메모리: 1 chunk 만큼만 (현 30일 = 7.7s)
- 단점: chunk 경계에서 strategy state reset → open position carried over X
  - LiveBreakout `_was_above`, BNF `_was_below`, NR7 `_entered_today` 등 리셋

### D. 종목 수 줄이기 (현 V1)
- top 10 으로 줄여서 1년치 = 2M items / 메모리 2GB
- 사용자 룰 `feedback_multi_symbol` (universe 좁힘 X) 위배되지만 backtest 용도 한정 OK

## 권고 (5/14+)

- **일상 backtest**: 분봉 90-180일 (실용 max)
- **장기 검증**: 일봉 696일 (실용 max, 2.7년)
- **1년+ 분봉 필요 시**: chunked (option C) 또는 종목 줄이기 (option D)
- **진짜 성능 필요**: streaming generator (option A) 구현 — Phase 2 에 포함

## 진행 상태

- [x] 한계 분석 doc (이 파일)
- [ ] Option A (streaming) 구현 — 메모리 절감 70%+
- [ ] Option B (lightweight model) — domain.py 큰 refactor
- [x] Option C (chunked) — 현 backtest 1회 runs 로 충분, 더 필요 시 추가
- [x] Option D (작은 universe) — 분봉 400일 top 10 검증 완료
